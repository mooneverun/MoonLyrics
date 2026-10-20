import threading
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtTest import QTest

from moonlyrics.lyrics import Lyrics, LyricLine, SearchCancelled, Track
from moonlyrics.lyrics_worker import LyricsWorker


class WorkerTests(TestCase):
    def run_worker(self, finder, expected_results, delays=(.01, .02), after_result=None, source_order=None):
        app = QCoreApplication.instance() or QCoreApplication([])
        results = []
        with patch("moonlyrics.lyrics_worker.LyricsFinder", return_value=finder):
            worker = LyricsWorker()
            worker.retry_delays = delays
            def receive(*result):
                results.append(result)
                if after_result:
                    after_result(worker, result)
            worker.result_ready.connect(receive)
            worker.start()
            try:
                worker.request(1, Track("旧歌", "歌手", duration=100), "tr", source_order=source_order)
                deadline = time.monotonic() + 2
                while len(results) < expected_results and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertEqual(len(results), expected_results)
            finally:
                worker.stop()
                self.assertTrue(worker.wait(1000))
        finder.close.assert_called_once()
        return results

    def test_initial_miss_is_retried_and_recovers_without_user_action(self):
        document = Lyrics((LyricLine(1, "月光"),), "测试")
        finder = Mock()
        finder.find.side_effect = [(None, "暂未找到歌词"), (document, "已匹配")]
        results = self.run_worker(finder, 2)
        self.assertIn("自动重试", results[0][2])
        self.assertIs(results[1][1], document)
        self.assertEqual(finder.find.call_count, 2)
        self.assertFalse(finder.find.call_args_list[1].args[3], "Automatic retry must preserve verified metadata")

    def test_structural_miss_is_reported_without_repeating_the_same_search(self):
        finder = Mock(retryable=False)
        finder.find.return_value = (None, "未找到可靠的同步歌词")
        results = self.run_worker(finder, 1)
        self.assertEqual(finder.find.call_count, 1)
        self.assertNotIn("自动重试", results[0][2])

    def test_missing_lyrics_have_a_bounded_retry_count(self):
        finder = Mock()
        finder.find.return_value = (None, "未找到可靠的同步歌词")
        results = self.run_worker(finder, 3)
        self.assertEqual(finder.find.call_count, 3)
        self.assertTrue(all(result[1] is None for result in results))
        self.assertNotIn("自动重试", results[-1][2])

    def test_failed_search_exception_can_recover_on_retry(self):
        document = Lyrics((LyricLine(1, "月光"),), "测试")
        finder = Mock()
        finder.find.side_effect = [RuntimeError("temporary failure"), (document, "已匹配")]
        with self.assertLogs("moonlyrics.lyrics", level="ERROR"):
            results = self.run_worker(finder, 2)
        self.assertIs(results[-1][1], document)

    def test_shutdown_interrupts_a_scheduled_retry(self):
        finder = Mock()
        finder.find.return_value = (None, "暂未找到")
        self.run_worker(finder, 1, delays=(10, 10), after_result=lambda worker, result: worker.stop())
        self.assertEqual(finder.find.call_count, 1)

    def test_new_song_interrupts_a_scheduled_retry_immediately(self):
        document = Lyrics((LyricLine(1, "月光"),), "测试")
        finder = Mock()
        finder.find.side_effect = lambda track, *args: (document, "已匹配") if track.title == "新歌" else (None, "暂未找到")
        def after_result(worker, result):
            if result[0] == 1:
                worker.request(2, Track("新歌", "歌手", duration=100), "tr")
        results = self.run_worker(finder, 2, delays=(10, 10), after_result=after_result)
        self.assertEqual([result[0] for result in results], [1, 2])
        self.assertIs(results[-1][1], document)
        self.assertEqual(finder.find.call_count, 2)

    def test_new_song_cancels_inflight_search_and_is_the_only_result(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        entered, release = threading.Event(), threading.Event()
        document = Lyrics((LyricLine(1, "月光"),), "测试")
        finder = Mock()

        def find(track, storefront, cancelled, force, source_order):
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

    def test_retries_preserve_the_source_order_snapshot(self):
        order = ["qq", "netease", "kugou", "lrclib"]
        expected = tuple(order)
        finder = Mock()
        finder.find.side_effect = [(None, "暂未找到"), (Lyrics((LyricLine(1, "月光"),), "测试"), "已匹配")]
        self.run_worker(finder, 2, source_order=order, after_result=lambda worker, result: order.reverse())
        self.assertEqual([call.args[4] for call in finder.find.call_args_list], [expected, expected])

    def test_changing_priority_interrupts_the_old_scheduled_retry(self):
        new_order = ("netease", "qq", "kugou", "lrclib")
        document = Lyrics((LyricLine(1, "月光"),), "网易云音乐")
        finder = Mock()
        finder.find.side_effect = lambda track, store, cancel, force, order: (document, "已匹配") if order == new_order else (None, "暂未找到")
        def after_result(worker, result):
            if result[0] == 1:
                worker.request(2, Track("旧歌", "歌手", duration=100), "tr", True, new_order)
        results = self.run_worker(finder, 2, delays=(10, 10), after_result=after_result)
        self.assertEqual([result[0] for result in results], [1, 2])
        self.assertIs(results[-1][1], document)
        self.assertEqual(finder.find.call_count, 2)

    def test_cache_clear_cancels_inflight_results_and_runs_on_the_worker(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        entered, release = threading.Event(), threading.Event()
        finder = Mock()
        finder.clear_cache.return_value = True
        def find(track, storefront, cancelled, force, source_order):
            entered.set()
            if not release.wait(1):
                raise RuntimeError("Test did not release search")
            if cancelled():
                raise SearchCancelled()
            return None, "未找到可靠的同步歌词"
        finder.find.side_effect = find
        results, cleared, clear_threads = [], [], []
        finder.clear_cache.side_effect = lambda: clear_threads.append(threading.get_ident()) or True
        with patch("moonlyrics.lyrics_worker.LyricsFinder", return_value=finder) as factory:
            worker = LyricsWorker(cache_dir="isolated-cache")
            worker.result_ready.connect(lambda *result: results.append(result))
            worker.cache_cleared.connect(cleared.append)
            worker.start()
            try:
                worker.request(1, Track("月光", "歌手", duration=100), "tr")
                self.assertTrue(entered.wait(1))
                worker.clear_cache()
                release.set()
                deadline = time.monotonic() + 2
                while not cleared and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertEqual(cleared, [True])
                self.assertEqual(results, [])
                self.assertNotEqual(clear_threads, [threading.get_ident()])
                factory.assert_called_once_with(cache_dir="isolated-cache")
                finder.clear_cache.assert_called_once()
            finally:
                release.set(); worker.stop()
                self.assertTrue(worker.wait(1000))
        finder.close.assert_called_once()
