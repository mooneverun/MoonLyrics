"""Desktop capture failures must recover without overriding an explicit pause."""
import ctypes as C
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if sys.platform == "win32":
    from PyQt6.QtCore import QEvent, QPoint, QTimer
    from PyQt6.QtGui import QMoveEvent
    from PyQt6.QtWidgets import QApplication
    from moonlyrics import windows as native
    from moonlyrics import windows_capture as capture
    from moonlyrics.playback import PlaybackSnapshot
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == "win32", "Windows desktop capture")
class CaptureRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        logs = patch.object(native.LOG, "exception")
        logs.start()
        self.addCleanup(logs.stop)
        self.control = native.Controls(dict(DEFAULTS), save=False, media=False)
        for timer in self.control.findChildren(QTimer):
            timer.stop()
        self.glass = self.control.glass
        self.visible = patch.object(self.glass, "isVisible", return_value=True)
        self.visible.start()
        self.addCleanup(self.visible.stop)

    def tearDown(self):
        for timer in self.control.findChildren(QTimer):
            timer.stop()
        self.glass.close()
        self.glass.deleteLater()
        self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def fail_capture(self):
        with patch.object(self.glass.view, "take_frame", side_effect=C.WinError(5)):
            self.glass.refresh()

    def test_access_denied_retries_and_rebuilds_before_resuming(self):
        self.fail_capture()
        self.assertFalse(self.glass.paused, "Access denied permanently paused background capture")
        self.assertTrue(self.glass.timer.isActive())
        self.assertEqual(self.glass.timer.interval(), 1000)
        self.assertIsNone(self.control.error)
        self.assertIn("自动恢复", self.control.status.text())
        self.assertEqual(self.control.pause.text(), "暂停刷新")
        with patch.object(self.glass.capture, "reset") as reset, \
                patch.object(self.glass.view, "take_frame") as take:
            self.glass.refresh()
            reset.assert_called_once_with()
            take.assert_called_once_with()
        self.assertEqual(self.glass.retry_delay, 0)
        self.assertEqual(self.glass.timer.interval(), round(1000 / self.control.settings["fps"]))
        self.assertIn("已恢复", self.control.status.text())
        self.assertNotEqual(self.control.status.objectName(), "errorStatus")

    def test_repeated_failures_back_off_and_fps_change_keeps_retry_rate(self):
        self.fail_capture()
        with patch.object(self.glass.capture, "reset", side_effect=C.WinError(5)):
            for expected in (2000, 4000, 5000, 5000):
                self.glass.refresh()
                self.assertEqual(self.glass.timer.interval(), expected)
        self.control.fps.setCurrentIndex(self.control.fps.findData(15))
        self.assertEqual(self.glass.timer.interval(), 5000)
        self.assertFalse(self.glass.paused)

    def test_manual_pause_blocks_retry_slider_and_playback_resume(self):
        self.fail_capture()
        self.control.toggle_pause()
        self.assertTrue(self.glass.paused)
        with patch.object(self.glass.capture, "reset") as reset, \
                patch.object(self.glass.view, "take_frame") as take:
            self.glass.refresh()
            self.control.sliders["width"].setValue(60)
            self.control.set_playback(PlaybackSnapshot(status="playing"))
            reset.assert_not_called()
            take.assert_not_called()
        self.assertFalse(self.glass.timer.isActive())
        with patch.object(self.glass.capture, "reset"), patch.object(self.glass.view, "take_frame") as take:
            self.control.toggle_pause()
            self.glass.refresh()
            take.assert_called_once_with()
        self.assertFalse(self.glass.paused)
        self.assertEqual(self.glass.retry_delay, 0)

    def test_playback_resume_nudges_pending_recovery(self):
        self.fail_capture()
        with patch.object(self.glass.capture, "reset"), patch.object(self.glass.view, "take_frame") as take:
            self.control.set_playback(PlaybackSnapshot(status="playing"))
            take.assert_called_once_with()
        self.assertEqual(self.glass.retry_delay, 0)

    def test_non_capture_failure_remains_fatal(self):
        with patch.object(self.glass.view, "take_frame", side_effect=RuntimeError("GPU failure")):
            self.glass.refresh()
        self.assertTrue(self.glass.paused)
        self.assertFalse(self.glass.timer.isActive())
        self.assertEqual(self.control.error, "GPU failure")
        self.assertEqual(self.control.pause.text(), "重试刷新")

    def test_moves_coalesce_a_refresh_without_overriding_pause_or_recovery_backoff(self):
        event = QMoveEvent(QPoint(100, 100), QPoint(90, 100))
        with patch.object(self.glass.view, "take_frame", return_value=True) as take:
            self.glass.moveEvent(event); self.glass.moveEvent(event)
            self.assertTrue(self.glass.move_refresh.isActive())
            self.app.processEvents()
            take.assert_called_once_with(refresh=False)
            take.reset_mock()
            for paused, retry in ((True, 0), (False, 1000)):
                self.glass.paused, self.glass.retry_delay = paused, retry
                self.glass.moveEvent(event); self.app.processEvents()
                take.assert_not_called()

    def test_only_dragging_requests_extra_pixels_and_a_missing_frame_stays_nonblocking(self):
        with patch.object(self.glass.capture, "capture", return_value=None) as sample:
            self.glass.drag_anchor = QPoint(100, 100)
            self.assertFalse(self.glass.view.take_frame())
            self.assertGreater(sample.call_args.kwargs["margin"], 0)
            self.glass.drag_anchor = None
            self.assertFalse(self.glass.view.take_frame())
            self.assertEqual(sample.call_args.kwargs, {})

    def test_capture_permission_failure_remains_fatal(self):
        with patch.object(native, "set_affinity", return_value=False):
            self.glass.protect()
        self.assertTrue(self.glass.paused)
        self.assertFalse(self.glass.timer.isActive())
        self.assertIn("可捕获", self.control.error)

    def test_initialization_failure_releases_the_partial_native_host(self):
        worker = capture._Sampler()
        with patch.object(capture, "mag_initialize", return_value=True), \
                patch.object(capture, "create_window", side_effect=[123, 0]), \
                patch.object(capture, "layer_alpha", return_value=True), \
                patch.object(capture, "destroy_window") as destroy, \
                patch.object(capture, "mag_uninitialize") as uninitialize:
            worker.run()
        self.assertIsNotNone(worker.error)
        destroy.assert_called_once_with(123)
        uninitialize.assert_called_once_with()

    def test_no_native_window_is_created_when_capture_runtime_is_unavailable(self):
        worker = capture._Sampler()
        with patch.object(capture, "mag_initialize", return_value=False), \
                patch.object(capture, "create_window") as create, \
                patch.object(capture, "mag_uninitialize") as uninitialize:
            worker.run()
        self.assertIsNotNone(worker.error)
        create.assert_not_called()
        uninitialize.assert_not_called()

    def test_moving_crop_reuses_a_covering_frame_at_the_current_screen_coordinates(self):
        sampler = capture.DesktopRegion()
        self.addCleanup(sampler.close)
        raw = bytes(channel for y in range(90, 120) for x in range(90, 130) for channel in (x, y, 77, 255))
        worker = SimpleNamespace(lock=threading.Lock(), error=None,
                                 latest=((123, 90, 90, 40, 30), 1, (raw, 160), 12.),
                                 wake=threading.Event(), stopping=threading.Event(), join=Mock(), is_alive=lambda: False)
        sampler.worker = worker
        location = [100, 100]
        def rectangle(hwnd, rect):
            rect._obj.right, rect._obj.bottom = 8, 4
            return True
        def origin(hwnd, point):
            point._obj.x, point._obj.y = location
            return True
        with patch.object(capture, "client_rect", side_effect=rectangle), \
                patch.object(capture, "client_origin", side_effect=origin), \
                patch.object(capture, "metrics", side_effect=lambda n: {76: 0, 77: 0, 78: 1920, 79: 1080}[n]):
            original = None
            for coordinate in (100, 103, 106, 109):
                location[:] = coordinate, coordinate
                self.assertEqual(sampler.capture(123, 1, margin=8, refresh=False), (8, 4, 1))
                self.assertEqual(sampler.size, (40, 30))
                ox, oy = sampler.offset
                row = sampler.size[1]-1-(oy+4) if sampler.top_down else oy+4
                top_left = sampler.bits.value+(row*sampler.size[0]+ox-1)*4
                self.assertEqual(tuple(C.string_at(top_left, 4)), (coordinate-1, coordinate-1, 77, 255))
                pixels = C.string_at(sampler.bits.value, 40*30*4)
                if original is not None:
                    self.assertEqual(pixels, original)
                    self.assertEqual(sampler.revision, 1)
                original = pixels
                if coordinate in (103, 106):
                    self.assertIsNone(worker.pending, "Motion queued another capture inside the fixed region")
                worker.pending = None
            # Fresh native frames keep the health heartbeat, but identical
            # pixels must not invalidate GPU textures or allocate a flipped copy.
            worker.latest = (worker.latest[0], 2, (raw, 160), 12.)
            self.assertIsNotNone(sampler.capture(123, 1, margin=8, refresh=False))
            self.assertEqual(sampler.revision, 1)
            self.assertIs(sampler.buffer, raw)
            self.assertEqual(C.string_at(sampler.bits, len(raw)), raw)
            changed = bytes([raw[0]^1])+raw[1:]
            worker.latest = (worker.latest[0], 3, (changed, 160), 12.)
            self.assertIsNotNone(sampler.capture(123, 1, margin=8, refresh=False))
            self.assertEqual(sampler.revision, 2)
            location[:] = 150, 100
            self.assertIsNone(sampler.capture(123, 1), "A non-covering frame was used at the wrong position")
            location[:] = 100, 100
            self.assertIsNone(sampler.capture(456, 1), "A different window's exclusion filter was reused")

    def test_finished_frame_is_published_even_if_the_next_position_is_already_queued(self):
        worker = capture._Sampler()
        first = (123, 0, 0, 10, 10)
        worker.pending = worker.requested = first
        worker.wake.set()
        def sample(*args):
            worker.raw = bytes(400), 40
            worker.requested = (123, 8, 0, 10, 10)
            worker.stopping.set()
            return True
        with patch.object(capture, "mag_initialize", return_value=True), \
                patch.object(capture, "create_window", side_effect=[1234, 1235]), \
                patch.object(capture, "layer_alpha", return_value=True), \
                patch.object(capture, "position_window", return_value=True), \
                patch.object(capture, "mag_callback", return_value=True), \
                patch.object(capture, "mag_filter", return_value=True), \
                patch.object(capture, "show_window"), \
                patch.object(capture, "mag_source", side_effect=sample), \
                patch.object(capture, "invalidate", return_value=True), \
                patch.object(capture, "update_window", return_value=True), \
                patch.object(capture, "destroy_window"), \
                patch.object(capture, "mag_uninitialize"):
            worker.run()
        self.assertIsNone(worker.error)
        self.assertIsNotNone(worker.latest, "Moving the target discarded a completed desktop frame")
        self.assertEqual(worker.latest[0], first)

    def test_shared_mailboxes_route_both_views_through_one_native_callback(self):
        worker = capture._Sampler()
        client = capture._SamplerClient(worker)
        primary, secondary = (123, 0, 0, 10, 10), (456, 20, 0, 10, 10)
        worker.pending = primary
        client.pending = secondary
        worker.excluded_windows=(123,456)
        worker.wake.set()
        received = []
        def sample(hwnd, rect):
            received.append(rect.left)
            worker.raw = bytes([rect.left])*400, 40
            if len(received) == 2:
                worker.stopping.set()
            return True
        with patch.object(capture, 'mag_initialize', return_value=True) as initialize, \
                patch.object(capture, 'create_window', side_effect=[1234, 1235]), \
                patch.object(capture, 'layer_alpha', return_value=True), \
                patch.object(capture, 'position_window', return_value=True), \
                patch.object(capture, 'mag_callback', return_value=True) as callback, \
                patch.object(capture, 'mag_filter', return_value=True) as filters, \
                patch.object(capture, 'show_window'), \
                patch.object(capture, 'mag_source', side_effect=sample), \
                patch.object(capture, 'invalidate', return_value=True), \
                patch.object(capture, 'update_window', return_value=True), \
                patch.object(capture, 'destroy_window'), \
                patch.object(capture, 'mag_uninitialize'):
            worker.run()
        initialize.assert_called_once(); callback.assert_called_once()
        filters.assert_called_once()
        self.assertEqual(tuple(filters.call_args.args[3]),(123,456,1234))
        self.assertIsNone(worker.error)
        self.assertEqual(worker.latest[0], primary)
        self.assertEqual(client.latest[0], secondary)
        self.assertEqual(received, [0, 20])

    def test_source_resize_and_motion_discard_provisional_native_frames(self):
        for target in ((456,20,0,20,12),(123,20,0,10,10)):
            with self.subTest(target=target):
                worker = capture._Sampler()
                client = capture._SamplerClient(worker)
                worker.pending = (123, 0, 0, 10, 10)
                client.pending = target
                worker.wake.set()
                sources = []
                def source(hwnd, rect):
                    sources.append(rect.left)
                    width, height = worker.dimensions
                    worker.raw = bytes(width*height*4), width*4
                    if len(sources) == 2: worker.stopping.set()
                    return True
                def redraw(hwnd):
                    width, height = worker.dimensions
                    worker.raw = bytes([255])*(width*height*4), width*4
                    return True
                with patch.object(capture, 'mag_initialize', return_value=True), \
                        patch.object(capture, 'create_window', side_effect=[1234, 1235]), \
                        patch.object(capture, 'layer_alpha', return_value=True), \
                        patch.object(capture, 'position_window', return_value=True), \
                        patch.object(capture, 'mag_callback', return_value=True), \
                        patch.object(capture, 'mag_filter', return_value=True), \
                        patch.object(capture, 'show_window'), \
                        patch.object(capture, 'mag_source', side_effect=source), \
                        patch.object(capture, 'invalidate', return_value=True), \
                        patch.object(capture, 'update_window', side_effect=redraw), \
                        patch.object(capture, 'destroy_window'), \
                        patch.object(capture, 'mag_uninitialize'):
                    worker.run()
                self.assertIsNone(worker.error)
                self.assertEqual(worker.latest[2][0][0], 255)
                self.assertEqual(client.latest[2][0][0], 255)

    def test_closing_a_settings_mailbox_does_not_stop_the_lyric_sampler(self):
        primary = capture.DesktopRegion()
        primary.worker = capture._Sampler()
        shared = capture.DesktopRegion(shared_with=primary)
        shared.worker = capture._SamplerClient(primary.worker)
        self.assertEqual(len(primary.worker.clients), 1)
        self.assertTrue(shared.close())
        self.assertIsNone(shared.worker)
        self.assertEqual(len(primary.worker.clients), 0)
        self.assertFalse(primary.worker.stopping.is_set())

    def test_stalled_sampling_recovers_without_accumulating_replacement_threads(self):
        sampler = capture.DesktopRegion()
        worker = SimpleNamespace(lock=threading.Lock(), error=None, latest=None, start=Mock(),
                                 wake=threading.Event(), stopping=threading.Event(), join=Mock(), is_alive=Mock(return_value=True))
        def rectangle(hwnd, rect):
            rect._obj.right, rect._obj.bottom = 320, 80
            return True
        with patch.object(capture, "_Sampler", return_value=worker) as factory, \
                patch.object(capture, "client_rect", side_effect=rectangle), \
                patch.object(capture, "client_origin", return_value=True), \
                patch.object(capture, "metrics", side_effect=lambda n: {76: 0, 77: 0, 78: 1920, 79: 1080}[n]):
            for stamp in (100, 100.8, 101.6, 102.4):
                with patch.object(capture.time, "perf_counter", return_value=stamp):
                    self.assertIsNone(sampler.capture(123, 0))
            with patch.object(capture.time, "perf_counter", return_value=103.2):
                with self.assertRaisesRegex(OSError, "未更新"):
                    sampler.capture(123, 0)
            # No polling during a manual pause must not count as a stalled sampler.
            with patch.object(capture.time, "perf_counter", return_value=500):
                self.assertIsNone(sampler.capture(123, 0))
            for _ in range(3):
                with self.assertRaises(OSError):
                    sampler.reset()
                self.assertIs(sampler.worker, worker)
            factory.assert_called_once()
            self.assertTrue(worker.stopping.is_set())
            worker.is_alive.return_value = False
            self.assertTrue(sampler.close())
            self.assertIsNone(sampler.worker)


if __name__ == "__main__":
    unittest.main()
