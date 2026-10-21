from datetime import datetime, timedelta, timezone
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from moonlyrics.windows_media import AppleMusicReader, select_apple_session
from moonlyrics.playback import PlaybackSnapshot


def session(source, status):
    return SimpleNamespace(source_app_user_model_id=source,
                           get_playback_info=lambda: SimpleNamespace(playback_status=status))


class WindowsMediaTests(unittest.TestCase):
    def test_controls_follow_native_capabilities(self):
        native = session("AppleMusic!App", 4)
        native.get_playback_info = lambda: SimpleNamespace(playback_status=4, playback_rate=1.,
            controls=SimpleNamespace(is_previous_enabled=False, is_play_pause_toggle_enabled=True, is_next_enabled=True))
        native.try_get_media_properties_async = lambda: SimpleNamespace(title="月光", artist="Singer", album_title="Album")
        native.get_timeline_properties = lambda: SimpleNamespace(position=timedelta(seconds=20),
            start_time=timedelta(), end_time=timedelta(seconds=200), last_updated_time=datetime.now(timezone.utc))
        reader = AppleMusicReader(); reader._get = lambda operation: operation
        self.assertEqual(reader._read(native).controls, ("toggle", "next"))

    def test_commands_recheck_the_target_capabilities_and_native_result(self):
        reader = AppleMusicReader(); reader._get = lambda operation: operation
        native = session("AppleMusic!App", 4)
        controls = SimpleNamespace(is_previous_enabled=True, is_play_pause_toggle_enabled=True, is_next_enabled=True)
        native.get_playback_info = lambda: SimpleNamespace(controls=controls)
        for action, method in (("previous", "try_skip_previous_async"), ("toggle", "try_toggle_play_pause_async"),
                               ("next", "try_skip_next_async")):
            operation = Mock(return_value=True); setattr(native, method, operation)
            self.assertEqual(reader._send_command(native, action), "")
            operation.assert_called_once_with()
        self.assertTrue(reader._send_command(None, "next"))
        browser = session("Microsoft.Edge", 4)
        self.assertTrue(reader._send_command(browser, "next"))
        controls.is_next_enabled = False
        self.assertTrue(reader._send_command(native, "next"))
        self.assertEqual(native.try_skip_next_async.call_count, 1)
        controls.is_next_enabled = True
        native.try_skip_next_async.return_value = False
        self.assertTrue(reader._send_command(native, "next"))
        native.try_skip_next_async.side_effect = TimeoutError()
        with patch("moonlyrics.windows_media.LOG.warning"):
            self.assertTrue(reader._send_command(native, "next"))
        with patch.object(reader, "isInterruptionRequested", return_value=True):
            self.assertEqual(reader._send_command(native, "toggle"), "播放控制已取消")
        self.assertEqual(native.try_toggle_play_pause_async.call_count, 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows worker integration")
    def test_command_runs_once_on_reader_thread_and_does_not_block_snapshot_updates(self):
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtTest import QTest
        import winrt.windows.media.control as projection
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        reader = AppleMusicReader()
        native = session("AppleMusic!App", 4)
        started, release = threading.Event(), threading.Event()
        calls, finished, snapshots = [], [], []
        def toggle():
            calls.append(threading.get_ident()); started.set()
            return release.wait(2.)
        native.get_playback_info = lambda: SimpleNamespace(playback_status=4,
            controls=SimpleNamespace(is_play_pause_toggle_enabled=True))
        native.try_toggle_play_pause_async = toggle
        manager = SimpleNamespace(get_sessions=lambda: [session("Microsoft.Edge", 4), native])
        reader._get = lambda operation: operation
        reader._subscribe = Mock(); reader._unsubscribe = Mock()
        reader._read = lambda current: PlaybackSnapshot(source=current.source_app_user_model_id, title="Test", status="playing")
        reader.command_finished.connect(lambda action, message: finished.append((action, message)))
        reader.snapshot_changed.connect(snapshots.append)
        def wait_for(predicate):
            deadline = time.perf_counter() + 3
            while not predicate() and time.perf_counter() < deadline:
                QTest.qWait(10)
            self.assertTrue(predicate())
        with patch.object(projection, "GlobalSystemMediaTransportControlsSessionManager",
                          SimpleNamespace(request_async=lambda: manager)):
            try:
                self.assertFalse(reader.request_command("next"))
                reader.start(); wait_for(lambda: bool(snapshots))
                self.assertFalse(reader.request_command("unknown"))
                self.assertTrue(reader.request_command("toggle"))
                wait_for(started.is_set)
                self.assertFalse(reader.request_command("next"), "A repeated click queued another command")
                release.set(); wait_for(lambda: bool(finished))
                self.assertEqual(finished, [("toggle", "")])
                self.assertEqual(len(calls), 1)
                self.assertNotEqual(calls[0], threading.get_ident())
                self.assertTrue(all(s.source == native.source_app_user_model_id for s in snapshots))
                wait_for(lambda: len(snapshots) >= 2)
            finally:
                release.set(); reader.stop(); self.assertTrue(reader.wait(2500))
        self.assertFalse(reader.request_command("toggle"))

    @unittest.skipUnless(sys.platform == "win32", "Windows worker integration")
    def test_queued_command_is_discarded_after_session_lookup_failure(self):
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtTest import QTest
        import winrt.windows.media.control as projection
        app = QApplication.instance() or QApplication([])
        reader = AppleMusicReader(); finished = []
        native = session("AppleMusic!App", 4)
        native.try_skip_next_async = Mock(return_value=True)
        manager = SimpleNamespace(get_sessions=lambda: [native])
        attempts = Mock(side_effect=[OSError("session closed"), manager])
        reader._get = lambda operation: operation
        reader._subscribe = Mock(); reader._unsubscribe = Mock()
        reader._read = lambda current: PlaybackSnapshot(source=current.source_app_user_model_id)
        reader._command = "next"
        reader.command_finished.connect(lambda action, message: finished.append((action, message)))
        with patch.object(projection, "GlobalSystemMediaTransportControlsSessionManager",
                          SimpleNamespace(request_async=attempts)), patch("moonlyrics.windows_media.LOG.warning"):
            try:
                reader.start()
                deadline = time.perf_counter() + 2.
                while (not finished or attempts.call_count < 2) and time.perf_counter() < deadline:
                    QTest.qWait(20)
                self.assertEqual(len(finished), 1)
                self.assertTrue(finished[0][1])
                self.assertIsNone(reader._command)
                native.try_skip_next_async.assert_not_called()
            finally:
                reader.stop(); self.assertTrue(reader.wait(2500))

    def test_other_players_never_replace_apple_music(self):
        browser, apple = session("Microsoft.Edge", 4), session("AppleInc.AppleMusicWin!App", 5)
        self.assertIs(select_apple_session([browser, apple]), apple)
        self.assertIsNone(select_apple_session([browser]))
        self.assertIsNone(select_apple_session([]))

    def test_playing_apple_session_has_priority(self):
        paused, playing = session("AppleMusic!A", 5), session("AppleMusic!B", 4)
        self.assertIs(select_apple_session([paused, playing]), playing)

    def test_paused_snapshot_reads_native_fields_and_resets_position(self):
        native = session("AppleMusic!App", 5)
        native.get_playback_info = lambda: SimpleNamespace(playback_status=5, playback_rate=None)
        native.try_get_media_properties_async = lambda: SimpleNamespace(title="月光", artist="Singer — Album", album_title="")
        native.get_timeline_properties = lambda: SimpleNamespace(position=timedelta(seconds=20),
            start_time=timedelta(), end_time=timedelta(seconds=200), last_updated_time=datetime.now(timezone.utc))
        reader = AppleMusicReader()
        reader._get = lambda operation: operation
        snapshot = reader._read(native)
        self.assertEqual((snapshot.title, snapshot.artist, snapshot.album), ("月光", "Singer", "Album"))
        self.assertEqual((snapshot.status, snapshot.position, snapshot.duration, snapshot.rate), ("paused", 20, 200, 1))
        self.assertEqual(snapshot.position_at(snapshot.observed_at + 2), 20)

    @unittest.skipUnless(sys.platform == "win32", "Windows projection is required")
    def test_native_operation_timeout_cancels_the_request(self):
        from winrt.windows.foundation import AsyncStatus
        operation = Mock()
        operation.wait.return_value = AsyncStatus.STARTED
        reader = AppleMusicReader()
        with patch("moonlyrics.windows_media.time.perf_counter", side_effect=[100, 103]):
            with self.assertRaises(TimeoutError):
                reader._get(operation)
        operation.cancel.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Windows projection is required")
    def test_shutdown_interrupts_a_pending_native_operation(self):
        from winrt.windows.foundation import AsyncStatus
        operation = Mock()
        operation.wait.return_value = AsyncStatus.STARTED
        reader = AppleMusicReader()
        with patch.object(reader, "isInterruptionRequested", return_value=True):
            with self.assertRaises(InterruptedError):
                reader._get(operation)
        operation.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
