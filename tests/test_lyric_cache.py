"""Persistent cache behavior with synthetic timed lyrics and isolated user data."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from moonlyrics.lyrics import LyricCandidate, Lyrics, LyricsFinder, LyricLine, SearchCancelled, Track
from moonlyrics.styles import DEFAULTS, lyric_offset_seconds


class LyricCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.track = Track("月光 (feat. 별)", "Luna", "Moon - Single", 180.123)
        self.document = Lyrics((LyricLine(.25, "月光"), LyricLine(2.5, "별")), "酷狗", self.track.title, self.track.artist)
        self.order = DEFAULTS["lyric_source_order"]

    def finder(self, success=True):
        finder = LyricsFinder(Path(self.directory.name) / "lyrics")
        self.addCleanup(finder.close)
        for source in self.order:
            setattr(finder, "_" + source, Mock(return_value=None))
        finder._aliases = Mock(side_effect=lambda track, store: [track])
        finder._album_aliases = Mock(return_value=None)
        finder._catalog_aliases = Mock(return_value=None)
        if success:
            def matched(*args, **kwargs):
                finder.selected_candidate = LyricCandidate("kugou", "synthetic-id", self.track, (), {"accesskey": "never-save-this"})
                return self.document
            finder._kugou.side_effect = matched
        return finder

    def seed(self):
        finder = self.finder()
        self.assertEqual(finder.find(self.track)[0], self.document)
        self.assertTrue(finder.cache_path.is_file())
        return finder

    def test_restart_reads_without_network_and_does_not_apply_calibration(self):
        first = self.seed()
        first.close()
        second = self.finder(False)
        found, message = second.find(self.track)
        self.assertEqual(found, self.document)
        self.assertIn("缓存", message)
        self.assertEqual(second.diagnostics["outcome"], "disk_cached")
        self.assertEqual(second.diagnostics["requests"], 0)
        second._kugou.assert_not_called()
        self.assertEqual(found.lines[0].time, .25)
        self.assertEqual(lyric_offset_seconds(DEFAULTS), 1.)
        self.assertEqual(found.index_at(1.5 + lyric_offset_seconds(DEFAULTS)), 1)

    def test_cache_is_opt_in_for_tests_and_stores_no_source_access_keys(self):
        finder = self.seed()
        with closing(sqlite3.connect(finder.cache_path)) as db, db:
            raw, = db.execute("SELECT data FROM lyrics").fetchone()
        self.assertNotIn("accesskey", raw)
        self.assertNotIn("never-save-this", raw)
        self.assertEqual(json.loads(raw)["selected"]["identifier"], "synthetic-id")
        without_disk = LyricsFinder()
        self.addCleanup(without_disk.close)
        self.assertIsNone(without_disk.cache_path)

    def test_identity_preserves_album_guest_version_region_order_and_duration(self):
        first = self.seed()
        cases = [replace(self.track, title="月光 (Live)"), replace(self.track, title="月光 (feat. Moon)"),
                 replace(self.track, artist="Other"), replace(self.track, album="Other"),
                 replace(self.track, duration=182.3)]
        for track in cases:
            with self.subTest(track=track):
                self.assertIsNone(first._cached_lyrics(track, "tr", self.order))
        self.assertIsNone(first._cached_lyrics(self.track, "tw", self.order))
        self.assertIsNone(first._cached_lyrics(self.track, "tr", tuple(reversed(self.order))))
        self.assertEqual(first._cached_lyrics(replace(self.track, duration=180.124), "tr", self.order), self.document)
        self.assertEqual(first._cached_lyrics(replace(self.track, title="月光 (FEAT. 별)  "), "tr", self.order), self.document)

    def test_priority_change_searches_again_instead_of_reusing_other_order(self):
        self.seed()
        second = self.finder()
        order = ("qq", "kugou", "netease", "lrclib")
        found, _ = second.find(self.track, source_order=order)
        self.assertEqual(found, self.document)
        second._qq.assert_called()
        second._kugou.assert_called_once()
        self.assertEqual(second.diagnostics["outcome"], "matched")

    def test_force_refresh_replaces_success_and_failure_preserves_previous_success(self):
        first = self.seed()
        updated = replace(self.document, lines=(LyricLine(1, "新月光"), LyricLine(3, "새 별")))
        self.document = updated
        first.find(self.track, force=True)
        self.assertEqual(self.finder(False).find(self.track)[0], updated)
        failed = self.finder(False)
        self.assertIsNone(failed.find(self.track, force=True)[0])
        self.assertEqual(self.finder(False).find(self.track)[0], updated)

    def test_miss_never_creates_a_negative_cache_entry(self):
        failed = self.finder(False)
        self.assertIsNone(failed.find(self.track)[0])
        self.assertFalse(failed.cache_path.exists())
        successful = self.finder()
        self.assertIsNotNone(successful.find(self.track)[0])
        successful._kugou.assert_called_once()

    def test_old_cache_and_future_matching_versions_are_rechecked(self):
        first = self.seed()
        with patch("moonlyrics.lyrics.time.time", return_value=1):
            with closing(sqlite3.connect(first.cache_path)) as db, db:
                db.execute("UPDATE lyrics SET saved=1")
        self.assertIsNone(first._cached_lyrics(self.track, "tr", self.order))
        first.find(self.track, force=True)
        with patch("moonlyrics.lyrics.__version__", "0.1.2"):
            self.assertIsNone(first._cached_lyrics(self.track, "tr", self.order))

    def test_malformed_timing_is_ignored_and_fresh_result_repairs_the_entry(self):
        first = self.seed()
        bad = ["not json", "[]", json.dumps(dict(source="酷狗", title="月光", artist="Luna", lines=[[1, "a"], [0, "b"]])),
               json.dumps(dict(source="酷狗", title="月光", artist="Luna", lines=[[1, "a"], [float("nan"), "b"]])),
               json.dumps(dict(source="酷狗", title="月光", artist="Luna", lines=[[1, "a"], [300, "b"]]))]
        for raw in bad:
            with self.subTest(raw=raw):
                with closing(sqlite3.connect(first.cache_path)) as db, db:
                    db.execute("UPDATE lyrics SET data=?", (raw,))
                second = self.finder()
                second.find(self.track)
                second._kugou.assert_called_once()
                self.assertEqual(self.finder(False).find(self.track)[0], self.document)

    def test_corrupt_database_and_denied_writes_never_break_online_matching(self):
        finder = self.finder()
        finder.cache_path.parent.mkdir(parents=True)
        finder.cache_path.write_bytes(b"corrupt sqlite")
        with self.assertLogs("moonlyrics.lyrics", level="WARNING"):
            self.assertEqual(finder.find(self.track)[0], self.document)
        self.assertTrue(finder.clear_cache())
        with patch("moonlyrics.lyrics.sqlite3.connect", side_effect=sqlite3.OperationalError("denied")):
            with self.assertLogs("moonlyrics.lyrics", level="WARNING"):
                self.assertEqual(finder.find(self.track, force=True)[0], self.document)

    def test_bounded_lru_and_content_size_only_load_the_current_lyrics(self):
        finder = self.seed()
        with patch("moonlyrics.lyrics.CACHE_LIMIT", 2):
            for index in (1, 2):
                finder.find(replace(self.track, title=f"Synthetic {index}"))
            self.assertIsNone(finder._cached_lyrics(self.track, "tr", self.order))
            kept = replace(self.track, title="Synthetic 1")
            self.assertIsNotNone(finder._cached_lyrics(kept, "tr", self.order))
            finder.find(replace(self.track, title="Synthetic 3"))
            self.assertIsNone(finder._cached_lyrics(replace(self.track, title="Synthetic 2"), "tr", self.order))
            self.assertIsNotNone(finder._cached_lyrics(kept, "tr", self.order))
        with patch("moonlyrics.lyrics.CACHE_BYTES", 1):
            finder.find(replace(self.track, title="Synthetic 4"))
        with closing(sqlite3.connect(finder.cache_path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM lyrics").fetchone()[0], 0)

    def test_clear_removes_disk_and_memory_but_does_not_modify_current_document(self):
        finder = self.seed()
        self.assertTrue(finder.clear_cache())
        self.assertFalse(finder.cache_path.exists())
        self.assertFalse(finder.cache)
        self.assertEqual(self.document.lines[0].text, "月光")
        finder.find(self.track)
        self.assertEqual(finder._kugou.call_count, 2)
        with patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            with self.assertLogs("moonlyrics.lyrics", level="WARNING"):
                self.assertFalse(finder.clear_cache())

    def test_cancelled_search_never_publishes_or_creates_cache(self):
        finder = self.finder()
        with self.assertRaises(SearchCancelled):
            finder.find(self.track, cancelled=lambda: True)
        self.assertFalse(finder.cache_path.exists())


if __name__ == "__main__":
    unittest.main()
