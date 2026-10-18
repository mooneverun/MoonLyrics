import unittest

from moonlyrics.playback import PlaybackSnapshot, apple_metadata, format_time, timeline_anchor


class PlaybackTests(unittest.TestCase):
    def test_clock_advances_while_playing_and_freezes_when_paused(self):
        playing = PlaybackSnapshot(title="Song", status="playing", position=60, duration=200, observed_at=100)
        paused = PlaybackSnapshot(title="Song", status="paused", position=60, duration=200, observed_at=100)
        self.assertAlmostEqual(playing.position_at(100.25), 60.25)
        self.assertEqual(paused.position_at(102), 60)
        self.assertEqual(playing.position_at(110), 63)  # Stale feed stops extrapolating.

    def test_new_snapshot_resets_clock_after_seek_and_track_change(self):
        before = PlaybackSnapshot(title="A", status="playing", position=90, observed_at=100)
        seek = PlaybackSnapshot(title="A", status="playing", position=10, observed_at=101)
        next_track = PlaybackSnapshot(title="B", status="playing", position=0, observed_at=102)
        self.assertEqual(before.position_at(101), 91)
        self.assertEqual(seek.position_at(102), 11)
        self.assertEqual(next_track.position_at(103), 1)

    def test_duration_rate_and_backwards_clock_bounds(self):
        snapshot = PlaybackSnapshot(title="Song", status="playing", position=99, duration=100, rate=2, observed_at=100)
        self.assertEqual(snapshot.position_at(101), 100)
        self.assertEqual(snapshot.position_at(99), 99)

    def test_system_timestamp_correction_uses_playing_state_and_rate(self):
        self.assertEqual(timeline_anchor(20, 0, 100, 1000, 1000.5, "playing", 2), (21, 100))
        self.assertEqual(timeline_anchor(20, 0, 100, 1000, 1000.5, "paused", 2), (20, 100))
        self.assertEqual(timeline_anchor(30, 10, 110, 1000, 1000, "playing", 1), (20, 100))

    def test_unreliable_timestamp_and_duration_are_not_extrapolated(self):
        for timestamp in (0, 2000, float("inf")):
            self.assertEqual(timeline_anchor(20, 0, 100, timestamp, 1000, "playing", 1), (20, 100))
        self.assertEqual(timeline_anchor(200, 0, 100, 1000, 1000, "paused", 1), (100, 100))

    def test_chinese_and_foreign_names_are_preserved(self):
        self.assertEqual(apple_metadata("遠走高飛", "Sam Lee — 絕對痴心", ""), ("遠走高飛", "Sam Lee", "絕對痴心"))
        self.assertEqual(apple_metadata("Song", "A — B", "Other Album"), ("Song", "A — B", "Other Album"))
        self.assertEqual(apple_metadata(None, None, None), ("", "", ""))

    def test_time_format(self):
        self.assertEqual(format_time(64.9), "1:04")
        self.assertEqual(format_time(3661), "1:01:01")
        self.assertEqual(format_time(float("nan")), "0:00")


if __name__ == "__main__":
    unittest.main()
