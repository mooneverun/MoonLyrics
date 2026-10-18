from datetime import datetime, timedelta, timezone
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from moonlyrics.windows_media import AppleMusicReader, select_apple_session


def session(source, status):
    return SimpleNamespace(source_app_user_model_id=source,
                           get_playback_info=lambda: SimpleNamespace(playback_status=status))


class WindowsMediaTests(unittest.TestCase):
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
