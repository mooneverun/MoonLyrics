import threading
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtTest import QTest

from moonlyrics.lyrics import Lyrics, LyricLine, SearchCancelled, Track
from moonlyrics.lyrics_worker import LyricsWorker


class WorkerTests(TestCase):
    def test_new_song_cancels_inflight_search_and_is_the_only_result(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        entered, release = threading.Event(), threading.Event()
        document = Lyrics((LyricLine(1, "月光"),), "测试")
        finder = Mock()

        def find(track, storefront, cancelled, force):
            if track.title == "旧歌":
                entered.set()
                if not release.wait(1):
                    raise RuntimeError("Test did not release the old search")
                if cancelled():
                    raise SearchCancelled()
            return document, "已匹配"

        finder.find.side_effect = find
        results = []
        with patch("moonlyrics.lyrics_worker.LyricsFinder", return_value=finder):
            worker = LyricsWorker()
            worker.result_ready.connect(lambda *result: results.append(result))
            worker.start()
            try:
                worker.request(1, Track("旧歌", "歌手", duration=100), "tr")
                self.assertTrue(entered.wait(1))
                worker.request(2, Track("新歌", "歌手", duration=100), "tr")
                release.set()
                deadline = time.monotonic() + 2
                while not results and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertEqual([result[0] for result in results], [2])
                self.assertIs(results[0][1], document)
                self.assertEqual(finder.find.call_count, 2)
            finally:
                release.set(); worker.stop()
                self.assertTrue(worker.wait(1000))
        finder.close.assert_called_once()
