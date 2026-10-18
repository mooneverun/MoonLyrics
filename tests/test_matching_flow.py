import sys
from unittest import TestCase, skipUnless
from unittest.mock import Mock


@skipUnless(sys.platform == "win32", "Windows Qt controller integration")
class MatchingFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from moonlyrics.styles import DEFAULTS
        from moonlyrics.windows import Controls
        self.control = Controls(dict(DEFAULTS), save=False, media=False)
        self.worker = self.control.lyrics_worker = Mock()
        self.worker.wait.return_value = True
        self.addCleanup(self.control.close)

    def snapshot(self, title="Moonlight", artist="Luna", duration=200.49, status="playing"):
        from moonlyrics.playback import PlaybackSnapshot
        return PlaybackSnapshot(title=title, artist=artist, album="Album", duration=duration, status=status)

    def test_duration_rounding_jitter_does_not_restart_a_song_search(self):
        self.control.set_playback(self.snapshot())
        first_request = self.control.lyric_request
        for duration in (200.51, 200.48, 200.52, 200.49):
            self.control.set_playback(self.snapshot(duration=duration))
        self.assertEqual(self.control.lyric_request, first_request)

    def test_new_song_waits_for_settled_metadata_before_network_search(self):
        from PyQt6.QtTest import QTest
        self.control.set_playback(self.snapshot(status="changing"))
        self.control.set_playback(self.snapshot(artist="Correct Artist", status="playing"))
        self.assertFalse(any(call.args[1] is not None for call in self.worker.request.call_args_list))
        QTest.qWait(750)
        jobs = [call.args[1] for call in self.worker.request.call_args_list if call.args[1] is not None]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].artist, "Correct Artist")

    def test_temporary_missing_duration_keeps_the_same_song_request(self):
        from PyQt6.QtTest import QTest
        self.control.set_playback(self.snapshot())
        request = self.control.lyric_request
        self.control.set_playback(self.snapshot(duration=0))
        self.assertEqual(self.control.lyric_request, request)
        QTest.qWait(750)
        jobs = [call.args[1] for call in self.worker.request.call_args_list if call.args[1] is not None]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].duration, 200.49)

    def test_rapid_skip_searches_only_the_latest_song_and_rejects_old_results(self):
        from PyQt6.QtTest import QTest
        from moonlyrics.lyrics import Lyrics, LyricLine
        self.control.set_playback(self.snapshot(title="Old Song"))
        stale = self.control.lyric_request
        self.control.set_playback(self.snapshot(title="New Song"))
        QTest.qWait(750)
        jobs = [call.args[1] for call in self.worker.request.call_args_list if call.args[1] is not None]
        self.assertEqual([job.title for job in jobs], ["New Song"])
        document = Lyrics((LyricLine(1, "月光"),), "测试")
        self.control.accept_lyrics(stale, document, "stale")
        self.assertIsNone(self.control.glass.view.lyrics)
        self.control.accept_lyrics(self.control.lyric_request, document, "current")
        self.assertIs(self.control.glass.view.lyrics, document)

    def test_new_song_without_duration_does_not_reuse_the_old_song_duration(self):
        from PyQt6.QtTest import QTest
        self.control.set_playback(self.snapshot())
        self.control.set_playback(self.snapshot(title="New Song", duration=0))
        QTest.qWait(750)
        self.assertFalse(any(call.args[1] is not None for call in self.worker.request.call_args_list))
        self.control.set_playback(self.snapshot(title="New Song", duration=150))
        QTest.qWait(750)
        jobs = [call.args[1] for call in self.worker.request.call_args_list if call.args[1] is not None]
        self.assertEqual([(job.title, job.duration) for job in jobs], [("New Song", 150)])

    def test_manual_retry_supersedes_the_pending_automatic_request(self):
        from PyQt6.QtTest import QTest
        self.control.set_playback(self.snapshot())
        self.control.request_lyrics(force=True)
        QTest.qWait(750)
        jobs = [call for call in self.worker.request.call_args_list if call.args[1] is not None]
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].args[3])
