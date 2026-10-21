"""Desktop capture failures must recover without overriding an explicit pause."""
import ctypes as C
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if sys.platform == "win32":
    from PyQt6.QtCore import QEvent, QTimer
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
        with patch.object(native.QSystemTrayIcon, "show"):
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
        self.control.tray.hide()
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
