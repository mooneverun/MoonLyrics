import base64
from unittest import TestCase
from unittest.mock import Mock

import requests

from moonlyrics.lyrics import Lyrics, LyricsFinder, LyricLine, SearchCancelled, Track, match_rank, normalize, parse_lrc, related_artist_name

SAMPLE = "[00:01.00]窗前的月光\n[00:03.50]慢慢经过身旁\n[00:06.00]\n[00:08.00]下一句轻轻唱"


class ParserTests(TestCase):
    def test_timestamps_repeats_empty_cues_and_word_tags(self):
        lines = parse_lrc("[ti:test]\n[00:03.5][00:08.500]<00:03.500>月光\n[00:01.00]开头\n[00:06.00]")
        self.assertEqual([(x.time, x.text) for x in lines], [(1, "开头"), (3.5, "月光"), (6, ""), (8.5, "月光")])
        document = Lyrics(lines)
        self.assertEqual([document.index_at(t) for t in (0, 1, 6, 8.5, 100)], [-1, 0, 2, 3, 3])

    def test_duplicate_translations_and_offset(self):
        lines = parse_lrc("[offset:+500]\n[00:02.00]月光\n[00:02.00]Moonlight\n[00:02.00]月光")
        self.assertEqual(lines, (LyricLine(1.5, "月光 / Moonlight"),))
        self.assertEqual(parse_lrc("[offset:-200]\n[00:01]A")[0].time, 1.2)

    def test_display_uses_simplified_chinese_without_changing_timing_or_english(self):
        lines = parse_lrc("[00:01.20]遠走高飛 / Don't look back!\n[00:03.45]這裡還有月光")
        self.assertEqual(lines, (LyricLine(1.2, "远走高飞 / Don't look back!"), LyricLine(3.45, "这里还有月光")))

    def test_scroll_arrives_at_cue_without_a_delayed_animation(self):
        document = Lyrics(tuple(LyricLine(t, "月光") for t in (60, 64, 68, 72)))
        self.assertEqual(document.scroll_at(67.4), 1.)
        self.assertAlmostEqual(document.scroll_at(67.725), 1.5)
        self.assertEqual(document.scroll_at(68), 2.)
        samples = [document.scroll_at(67 + n / 100) for n in range(151)]
        self.assertTrue(all(b >= a for a, b in zip(samples, samples[1:])))
        self.assertLess(document.scroll_at(67.451) - document.scroll_at(67.45), .00001)
        self.assertLess(document.scroll_at(68) - document.scroll_at(67.999), .00001)

    def test_short_cues_and_first_last_line_are_bounded(self):
        document = Lyrics((LyricLine(1, "月光"), LyricLine(1.2, "轻轻"), LyricLine(1.3, "落下")))
        self.assertEqual(document.scroll_at(0), -1.)
        for index, line in enumerate(document.lines):
            self.assertEqual(document.scroll_at(line.time), float(index))
        self.assertEqual(document.scroll_at(100), 2.)
        self.assertEqual(Lyrics(()).scroll_at(0), -1.)

    def test_unsynced_and_invalid_input_are_rejected(self):
        for text in (None, "Plain lyrics", "[ar:Singer]", "[00:99]bad", "[00:00]", "x" * 1_000_001):
            self.assertEqual(parse_lrc(text), ())


class MatchingTests(TestCase):
    def test_scripts_punctuation_case_and_accents(self):
        self.assertEqual(normalize("遠走高飛（重製）"), normalize("远走高飞(重制)"))
        self.assertEqual(normalize("Beyoncé"), normalize("BEYONCE"))
        expected = Track("遠走高飛", "李聖傑", duration=270)
        self.assertIsNotNone(match_rank(Track("远走高飞", "李圣杰", duration=269.9), [expected]))

    def test_cover_live_and_wrong_duration_are_rejected(self):
        expected = Track("远走高飞", "李圣杰", duration=270)
        for candidate in (Track("远走高飞", "金志文", duration=270), Track("远走高飞 (Live)", "李圣杰", duration=270),
                          Track("远走高飞", "李圣杰", duration=300), Track("远走高飞", "李圣杰", duration=0)):
            self.assertIsNone(match_rank(candidate, [expected]))

    def test_remaster_is_allowed_but_no_artist_guessing(self):
        target = Track("Yellow (2011 Remastered)", "Coldplay", duration=266)
        self.assertIsNotNone(match_rank(Track("Yellow", "Coldplay", duration=267), [target]))
        self.assertIsNone(match_rank(Track("江南", "林俊杰", duration=267), [Track("江南", "JJ Lin", duration=267)]))

    def test_stage_name_relation_is_only_a_conservative_candidate_filter(self):
        for left, right in (("連麻", "连麻Swimming"), ("艾志恒", "艾志恒Asen"), ("邓紫棋", "G.E.M.邓紫棋")):
            self.assertTrue(related_artist_name(left, right))
        for left, right in (("林俊杰", "林俊林"), ("王", "王Luna"), ("连麻", "连麻Cover"),
                            ("连麻", "连麻 & Swimming"), ("连麻Swimming", "连麻Moon"), ("JJ Lin", "林俊杰")):
            self.assertFalse(related_artist_name(left, right))


class FinderTests(TestCase):
    def setUp(self):
        self.finder = LyricsFinder()
        self.addCleanup(self.finder.close)

    def apple_record(self, identifier, title, artist, duration=270000):
        return dict(kind="song", trackId=identifier, trackName=title, artistName=artist,
                    collectionName="Album", trackTimeMillis=duration)

    def test_same_apple_id_unlocks_localized_names(self):
        source = Track("Far Away", "Sam Lee", duration=270)
        self.finder._json = Mock(side_effect=[
            dict(results=[self.apple_record(123, "Far Away", "Sam Lee")]),
            dict(results=[self.apple_record(999, "错误曲目", "其他歌手"), self.apple_record(123, "遠走高飛", "李聖傑")]),
        ])
        self.finder._lrclib = Mock(return_value=None)
        document = Lyrics(parse_lrc(SAMPLE), "测试", "遠走高飛", "李聖傑")
        self.finder._kugou = Mock(side_effect=lambda aliases: document if len(aliases) > 1 else None)
        found, _ = self.finder.find(source)
        self.assertEqual(found, document)
        self.assertEqual(self.finder._kugou.call_args.args[0][1].artist, "李聖傑")
        self.assertEqual(self.finder._json.call_args.kwargs["id"], 123)

    def test_unrelated_apple_search_result_does_not_create_alias(self):
        self.finder._json = Mock(return_value=dict(results=[self.apple_record(123, "Other", "Someone")]))
        self.finder._kugou = Mock(return_value=None); self.finder._lrclib = Mock(return_value=None)
        found, _ = self.finder.find(Track("Far Away", "Sam Lee", duration=270))
        self.assertIsNone(found)
        self.assertEqual(self.finder._json.call_count, 1)

    def test_provider_failure_falls_back_to_another_source(self):
        document = Lyrics(parse_lrc(SAMPLE), "LRCLIB")
        self.finder._json = Mock(side_effect=requests.ConnectionError("unavailable"))
        self.finder._lrclib = Mock(return_value=document)
        found, _ = self.finder.find(Track("月光", "歌手", duration=100))
        self.assertEqual(found, document)

    def test_kugou_validates_metadata_before_downloading(self):
        payload = base64.b64encode(SAMPLE.encode()).decode()
        self.finder._json = Mock(side_effect=[dict(status=200, candidates=[
            None,
            dict(id="wrong", accesskey="test", song="月光", singer="翻唱歌手", duration=100000),
            dict(id="correct", accesskey="test", song="月光", singer="歌手", duration=100000)]), dict(content=payload)])
        found, _ = self.finder.find(Track("月光", "歌手", duration=100))
        self.assertTrue(found.lines)
        self.assertEqual(self.finder._json.call_args.kwargs["id"], "correct")
        self.assertEqual(self.finder._json.call_args_list[0].kwargs["keyword"], "歌手 - 月光")

    def test_catalog_resolves_artist_name_for_traditional_apple_metadata(self):
        track = Track("譚sir", "連麻", "譚sir - Single", 171)
        payload = base64.b64encode(SAMPLE.encode()).decode()
        def response(url, **params):
            if "songsearch.kugou.com" in url:
                return dict(status=1, data=dict(lists=[dict(SingerName="连麻Swimming", SingerId=[790800]),
                                                      dict(SingerName="连麻", SingerId=[17067849])]))
            if url.endswith("/download"):
                return dict(content=payload)
            candidates = [dict(id="cover", accesskey="test", song="谭sir", singer="连麻Cover", duration=171720),
                          dict(id="correct", accesskey="test", song="谭sir", singer="连麻Swimming", duration=171720)]
            return dict(status=200, candidates=candidates if params.get("keyword") == "连麻Swimming - 谭sir" else [])
        self.finder._json = Mock(side_effect=response)
        self.finder._lrclib = Mock(return_value=None)
        self.finder._aliases = Mock(return_value=[track])
        found, _ = self.finder.find(track)
        self.assertIsNotNone(found)
        self.assertEqual(found.artist, "连麻Swimming")
        self.assertEqual(self.finder._json.call_args.kwargs["id"], "correct")
        self.assertTrue(any("songsearch.kugou.com" in call.args[0] for call in self.finder._json.call_args_list))

    def mock_artist_catalog(self, track, records, duration=100300):
        payload = base64.b64encode(SAMPLE.encode()).decode()
        def response(url, **params):
            if "songsearch.kugou.com" in url:
                return dict(status=1, data=dict(lists=records))
            if url.endswith("/download"):
                return dict(content=payload)
            candidate = dict(id="correct", accesskey="test", song=track.title, singer="月光诗人Luna", duration=duration)
            return dict(status=200, candidates=[candidate] if params.get("keyword") == "月光诗人Luna - 月光" else [])
        self.finder._json = Mock(side_effect=response)
        self.finder._lrclib = Mock(return_value=None)
        self.finder._aliases = Mock(return_value=[track])

    def test_catalog_recognizes_an_unlisted_stage_name(self):
        track = Track("月光", "月光诗人", duration=100)
        self.mock_artist_catalog(track, [dict(SingerName="月光诗人Luna", SingerId=[123])])
        found, _ = self.finder.find(track)
        self.assertIsNotNone(found)
        self.assertEqual(found.artist, "月光诗人Luna")

    def test_catalog_rejects_covers_collaborations_unknown_ids_and_ambiguous_names(self):
        track = Track("月光", "月光诗人", duration=100)
        cases = [None, [], [None], ["malformed"], [dict(SingerName="月光诗人Cover", SingerId=[123])],
                 [dict(SingerName="月光诗人Luna、他人", SingerId=[123, 456])],
                 [dict(SingerName="月光诗人Luna", SingerId=[0])],
                 [dict(SingerName="月光诗人Luna", SingerId=[True])],
                 [dict(SingerName="其他诗人Luna", SingerId=[123])],
                 [dict(SingerName="月光诗人Luna", SingerId=[123]), dict(SingerName="月光诗人Moon", SingerId=[456])],
                 [dict(SingerName="月光诗人Luna", SingerId=[123]), dict(SingerName="月光诗人Luna", SingerId=[456])]]
        for records in cases:
            with self.subTest(records=records):
                self.mock_artist_catalog(track, records)
                found, _ = self.finder.find(track, force=True)
                self.assertIsNone(found)
                self.assertFalse(any(call.args[0].endswith("/download") for call in self.finder._json.call_args_list))

    def test_automatically_discovered_name_requires_a_closer_duration(self):
        track = Track("月光", "月光诗人", duration=100)
        self.mock_artist_catalog(track, [dict(SingerName="月光诗人Luna", SingerId=[123])], duration=103000)
        found, _ = self.finder.find(track)
        self.assertIsNone(found)

    def test_cache_prevents_repeated_network_search_and_can_be_retried(self):
        document = Lyrics(parse_lrc(SAMPLE), "测试")
        self.finder._kugou = Mock(return_value=document)
        track = Track("月光", "歌手", duration=100)
        self.finder.find(track); self.finder.find(track)
        self.assertEqual(self.finder._kugou.call_count, 1)
        self.finder.find(track, force=True)
        self.assertEqual(self.finder._kugou.call_count, 2)

    def test_cancelled_song_does_not_continue_queries(self):
        with self.assertRaises(SearchCancelled):
            self.finder.find(Track("月光", "歌手", duration=100), cancelled=lambda: True)
