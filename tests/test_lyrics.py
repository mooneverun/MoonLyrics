import base64
import time
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from moonlyrics.lyrics import Lyrics, LyricsFinder, LyricLine, SearchCancelled, Track, match_rank, normalize, parse_lrc, parse_synced_lrc, related_artist_name
from moonlyrics.styles import DEFAULTS

SAMPLE = "[00:01.00]窗前的月光\n[00:03.50]慢慢经过身旁\n[00:06.00]\n[00:08.00]下一句轻轻唱"


class ParserTests(TestCase):
    def test_synced_lyric_quality_rejects_placeholders_corrupt_offsets_and_outside_cues(self):
        for text in ("[00:00.00]暂无歌词", "[10:00]A\n[11:00]B", "[offset:" + "9" * 400 + "]\n" + SAMPLE):
            self.assertEqual(parse_synced_lrc(text, 100), ())
        self.assertEqual(parse_synced_lrc(SAMPLE, 100), parse_lrc(SAMPLE))

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
    def test_embedded_chinese_title_readings_match_in_both_directions(self):
        for titled, plain in (("飞(FEY)1999", "飞1999"), ("飛［FEY］1999", "飞1999"),
                               ("月(YUE)光", "月光"), ("爱 (AI) 2025", "爱2025")):
            left, right = Track(titled, "Luna", duration=100), Track(plain, "Luna", duration=100.95)
            self.assertIsNotNone(match_rank(left, [right]), titled)
            self.assertIsNotNone(match_rank(right, [left]), titled)

    def test_reading_normalization_preserves_versions_numbers_and_complete_artists(self):
        target = Track("飞(FEY)1999", "99 God", duration=98)
        for title in ("飞1998", "飞(Live)1999", "飞(Remix)1999", "飞(DJ)1999",
                      "飞(Instrumental)1999", "飞(Cover)1999", "飞1999 (feat. Guest)"):
            self.assertIsNone(match_rank(Track(title, "99 God", duration=98), [target]), title)
        self.assertIsNone(match_rank(Track("飞1999", "Another Artist", duration=98), [target]))
        self.assertIsNone(match_rank(Track("飞1999", "99 God", duration=118), [target]))
        self.assertIsNone(match_rank(Track("飞1999", "99 God", duration=0), [target]))
        for titled, plain in (("Fly(FEY)1999", "Fly1999"), ("飞(FEY)", "飞")):
            self.assertIsNone(match_rank(Track(titled, "Luna", duration=100), [Track(plain, "Luna", duration=100)]))

    def test_attached_and_nested_feature_credits_preserve_recording_versions(self):
        for title in ("Moonlight (feat.Guest)", "Moonlight [ft.Guest]", "Moonlight (with Guest)"):
            self.assertIsNotNone(match_rank(Track(title, "Luna", duration=100),
                                           [Track("Moonlight", "Luna & Guest", duration=100)]))
        nested = Track("Moonlight (feat. Guest (게스트))", "Luna", duration=100)
        self.assertIsNotNone(match_rank(Track("Moonlight", "Luna & Guest (게스트)", duration=100), [nested]))
        live = Track("Moonlight (Live) (feat. Guest)", "Luna", duration=100)
        self.assertIsNotNone(match_rank(Track("Moonlight (feat.Guest) (Live)", "Luna", duration=100), [live]))
        self.assertIsNone(match_rank(Track("Moonlight (feat.Guest)", "Luna", duration=100), [live]))

    def test_verified_locale_can_supply_a_missing_feature_credit(self):
        aliases = [Track("Moonlight", "Luna", duration=100),
                   Track("달빛 (feat. 게스트)", "루나", duration=100)]
        self.assertIsNotNone(match_rank(Track("Moonlight", "Luna & 게스트", duration=100), aliases))
        self.assertIsNone(match_rank(Track("Moonlight", "Luna", duration=100), aliases))

    def test_collaborator_order_and_separators_do_not_change_the_recording(self):
        expected = Track("Moonlight", "Singer A & Singer B", duration=100)
        for artist in ("Singer B, Singer A", "Singer A feat. Singer B", "Singer A ＆ Singer B"):
            self.assertIsNotNone(match_rank(Track("Moonlight", artist, duration=100), [expected]))
        for artist in ("Singer A", "Singer A & Singer C", "Singer A & Singer B & Singer C", "Singer ASinger B"):
            self.assertIsNone(match_rank(Track("Moonlight", artist, duration=100), [expected]))

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
    def test_kugou_and_lrclib_skip_placeholder_candidates(self):
        track = Track("月光", "歌手", duration=100)
        self.finder.cancelled = lambda: False; self.finder.deadline = time.monotonic() + 20
        bad = "[00:00.00]暂无歌词"
        self.finder._json = Mock(side_effect=[dict(status=200, candidates=[dict(id=str(i), accesskey="test",
            song=track.title, singer=track.artist, duration=100000) for i in (1, 2)]),
            dict(content=base64.b64encode(bad.encode()).decode()), dict(content=base64.b64encode(SAMPLE.encode()).decode())])
        self.assertEqual(len(self.finder._kugou([track]).lines), len(parse_lrc(SAMPLE)))
        self.assertEqual(self.finder._json.call_args.kwargs["id"], "2")
        self.finder._json = Mock(return_value=[dict(trackName=track.title, artistName=track.artist,
            duration=100, syncedLyrics=text) for text in (bad, SAMPLE)])
        self.assertEqual(len(self.finder._lrclib([track]).lines), len(parse_lrc(SAMPLE)))

    def test_album_fallback_survives_an_initial_song_search_error(self):
        track = Track("Moonlight", "Luna", "Album", 100)
        vocal = self.apple_record(42, track.title, track.artist, 100000); vocal["collectionId"] = 50
        self.finder._json = Mock(side_effect=[requests.ConnectionError("temporary"), dict(results=[vocal]),
            dict(results=[vocal]), dict(results=[self.apple_record(42, "달빛", "루나", 100000)])])
        self.finder.cancelled = lambda: False; self.finder.deadline = time.monotonic() + 20
        self.assertEqual(self.finder._aliases(track, "tr")[1].title, "달빛")

    def test_kugou_searches_without_feature_text_but_still_checks_all_people(self):
        track = Track("Moonlight (feat. Guest)", "Luna", duration=100)
        payload = base64.b64encode(SAMPLE.encode()).decode()
        def response(url, **params):
            if url.endswith("/download"):
                return dict(content=payload)
            return dict(status=200, candidates=[dict(id="good", accesskey="test", song="Moonlight",
                singer="Luna & Guest", duration=100000)] if params.get("keyword") == "Luna - Moonlight" else [])
        self.finder._json = Mock(side_effect=response)
        self.finder.cancelled = lambda: False; self.finder.deadline = time.monotonic() + 20
        self.assertIsNotNone(self.finder._kugou([track]))

    def test_lrclib_searches_base_title_for_featured_recordings(self):
        track = Track("Moonlight (feat. Guest)", "Luna", duration=100)
        self.finder._json = Mock(side_effect=lambda url, **params: [dict(trackName="Moonlight",
            artistName="Luna & Guest", duration=100, syncedLyrics=SAMPLE)] if params.get("track_name") == "Moonlight" else [])
        self.finder.cancelled = lambda: False; self.finder.deadline = time.monotonic() + 20
        self.assertIsNotNone(self.finder._lrclib([track]))

    def test_partial_verified_names_survive_a_later_region_timeout(self):
        track = Track("Moonlight", "Luna", duration=100)
        document = Lyrics(parse_lrc(SAMPLE), "测试")
        clock = [0.]
        def response(url, **params):
            if "search" in url:
                return dict(results=[self.apple_record(42, "Moonlight", "Luna", 100000)])
            if params.get("country") == "kr":
                return dict(results=[self.apple_record(42, "Moonlit Night", "Luna", 100000)])
            clock[0] = self.finder.stage_deadline + .01
            self.finder._check()
        self.finder._json = Mock(side_effect=response)
        self.finder._kugou = Mock(side_effect=lambda aliases, **kwargs: document if len(aliases) > 1 else None)
        self.finder._lrclib = Mock(return_value=None)
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        with patch("moonlyrics.lyrics.time.monotonic", side_effect=lambda: clock[0]):
            found, _ = self.finder.find(track)
        self.assertIs(found, document)

    def test_chinese_aliases_do_not_prevent_checking_the_korean_locale(self):
        track = Track("月光", "露娜", duration=100)
        def response(url, **params):
            title, artist = ("달빛", "루나") if params.get("country") == "kr" else (track.title, track.artist)
            return dict(results=[self.apple_record(42, title, artist, 100000)])
        self.finder._json = Mock(side_effect=response)
        self.finder.cancelled = lambda: False; self.finder.deadline = time.monotonic() + 20
        self.assertTrue(any(alias.title == "달빛" for alias in self.finder._aliases(track, "tr")))

    def test_bad_first_download_does_not_hide_a_valid_second_candidate(self):
        track = Track("月光", "歌手", duration=100)
        candidates = [dict(id=str(i), accesskey="test", song="月光", singer="歌手", duration=100000) for i in range(2)]
        self.finder._json = Mock(side_effect=[dict(status=200, candidates=candidates), dict(content="not-base64!"),
                                             dict(content=base64.b64encode(SAMPLE.encode()).decode())])
        self.finder._lrclib = Mock(return_value=None)
        self.finder._aliases = Mock(return_value=[track])
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        document, _ = self.finder.find(track)
        self.assertIsNotNone(document)
        self.assertEqual(self.finder._json.call_args.kwargs["id"], "1")

    def test_partial_service_failure_is_not_reported_as_a_complete_empty_search(self):
        track = Track("月光", "歌手", duration=100)
        outage = [True]
        def get(url, **kwargs):
            if "lrclib" in url and outage[0]:
                raise requests.ConnectionError("temporary")
            if "lrclib" in url:
                payload = []
            elif "itunes" in url:
                payload = dict(results=[])
            elif "songsearch" in url:
                payload = dict(status=1, data=dict(lists=[]))
            else:
                payload = dict(status=200, candidates=[])
            response = Mock(status_code=200, content=b"{}")
            response.json.return_value = payload
            return response
        self.finder.session.get = Mock(side_effect=get)
        document, message = self.finder.find(track)
        self.assertIsNone(document)
        self.assertIn("查询暂未完成", message)
        outage[0] = False
        document, message = self.finder.find(track)
        self.assertIsNone(document)
        self.assertEqual(message, "未找到可靠的同步歌词")

    def test_failed_name_lookup_does_not_poison_later_automatic_search(self):
        track = Track("Moonlight", "Luna", duration=100)
        document = Lyrics(parse_lrc(SAMPLE), "测试")
        self.finder._json = Mock(side_effect=[requests.ConnectionError("temporary"),
            dict(results=[self.apple_record(42, "Moonlight", "Luna", 100000)]),
            dict(results=[self.apple_record(42, "月光", "露娜", 100000)])])
        self.finder._kugou = Mock(side_effect=lambda aliases, **kwargs: document if len(aliases) > 1 else None)
        self.finder._lrclib = Mock(return_value=None)
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        first, _ = self.finder.find(track)
        second, _ = self.finder.find(track)
        self.assertIsNone(first)
        self.assertIs(second, document)

    def test_slow_primary_source_cannot_starve_the_backup_source(self):
        track = Track("月光", "Artist", duration=100)
        local = Track("月光", "歌手", duration=100)
        self.finder._aliases = Mock(return_value=[track, local])
        clock = [0.]
        def get(url, params, timeout):
            clock[0] += min(.2 if "lrclib" in url else 6., sum(timeout))
            if "lrclib" in url:
                payload = [dict(trackName="月光", artistName="歌手", duration=100, syncedLyrics=SAMPLE)]
            elif url.endswith("/download"):
                payload = dict(content="")
            else:
                payload = dict(status=200, candidates=[dict(id=str(i), accesskey="test", song="月光", singer="歌手", duration=100000) for i in range(2)])
            response = Mock(status_code=200, content=b"{}")
            response.json.return_value = payload
            return response
        self.finder.session.get = Mock(side_effect=get)
        with patch("moonlyrics.lyrics.time.monotonic", side_effect=lambda: clock[0]):
            document, _ = self.finder.find(track)
        self.assertIsNotNone(document)
        self.assertEqual(document.source, "LRCLIB")

    def test_title_only_fallback_still_validates_the_entire_artist_list(self):
        track = Track("Moonlight", "Singer A & Singer B", duration=100)
        def response(url, **params):
            if params.get("artist_name"):
                return []
            return [dict(trackName="Moonlight", artistName="Singer A", duration=100, syncedLyrics=SAMPLE),
                    dict(trackName="Moonlight", artistName="Singer B, Singer A", duration=100, syncedLyrics=SAMPLE)]
        self.finder._json = Mock(side_effect=response)
        self.finder._kugou = Mock(return_value=None)
        self.finder._aliases = Mock(return_value=[track])
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        document, _ = self.finder.find(track)
        self.assertIsNotNone(document)
        self.assertEqual(document.artist, "Singer B, Singer A")

    def setUp(self):
        self.finder = LyricsFinder()
        self.addCleanup(self.finder.close)
        self.finder._netease = Mock(return_value=None)
        self.finder._qq = Mock(return_value=None)

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
        self.finder._kugou = Mock(side_effect=lambda aliases, **kwargs: document if len(aliases) > 1 else None)
        found, _ = self.finder.find(source)
        self.assertEqual(found, document)
        self.assertEqual(self.finder._kugou.call_args.args[0][1].artist, "李聖傑")
        self.assertEqual(self.finder._json.call_args.kwargs["id"], 123)

    def test_one_regional_lookup_failure_does_not_hide_the_other_region(self):
        track = Track("Far Away", "Sam Lee", duration=270)
        self.finder._json = Mock(side_effect=[
            dict(results=[self.apple_record(123, "Far Away", "Sam Lee")]),
            requests.ConnectionError("one region unavailable"),
            dict(results=[self.apple_record(123, "遠走高飛", "李聖傑")]),
        ])
        self.finder._lrclib = Mock(return_value=None)
        document = Lyrics(parse_lrc(SAMPLE), "测试")
        self.finder._kugou = Mock(side_effect=lambda aliases, **kwargs: document if len(aliases) > 1 else None)
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        found, _ = self.finder.find(track)
        self.assertIs(found, document)
        self.assertEqual(self.finder._json.call_args.kwargs["country"], "hk")

    def test_unrelated_apple_search_result_does_not_create_alias(self):
        self.finder._json = Mock(return_value=dict(results=[self.apple_record(123, "Other", "Someone")]))
        self.finder._kugou = Mock(return_value=None); self.finder._lrclib = Mock(return_value=None)
        found, _ = self.finder.find(Track("Far Away", "Sam Lee", duration=270))
        self.assertIsNone(found)
        self.assertEqual(self.finder._json.call_count, 1)

    def test_album_lookup_recovers_a_recording_missing_from_song_search(self):
        track = Track("Moonlight (feat. Guest)", "Luna", "Album", 100)
        vocal = self.apple_record(42, track.title, track.artist, 100000); vocal["collectionId"] = 50
        instrumental = self.apple_record(43, "Moonlight (Instrumental)", track.artist, 90000); instrumental["collectionId"] = 50
        localized = self.apple_record(42, "달빛 (feat. 게스트)", "루나", 100000)
        def response(url, **params):
            if params.get("term") == "Luna Album":
                return dict(results=[instrumental])
            if params.get("id") == 50:
                return dict(results=[instrumental, vocal])
            if params.get("id") == 42:
                return dict(results=[localized])
            return dict(results=[])
        self.finder._json = Mock(side_effect=response)
        document = Lyrics(parse_lrc(SAMPLE), "测试")
        self.finder._kugou = Mock(side_effect=lambda aliases, **kwargs: document if len(aliases) > 1 else None)
        self.finder._lrclib = Mock(return_value=None)
        found, _ = self.finder.find(track)
        self.assertIs(found, document)
        self.assertEqual(self.finder._kugou.call_args.args[0][1].title, localized["trackName"])
        self.assertEqual(self.finder._json.call_args.kwargs["id"], 42)

    def test_album_fallback_rejects_unrelated_albums_and_wrong_recordings(self):
        track = Track("Moonlight (feat. Guest)", "Luna", "Album", 100)
        for album in (dict(collectionId=50, collectionName="Other Album", artistName="Luna"),
                      dict(collectionId=50, collectionName="Album", artistName="Other Artist"),
                      dict(collectionId=True, collectionName="Album", artistName="Luna")):
            self.finder._json = Mock(side_effect=[dict(results=[]), dict(results=[album])])
            self.finder.cancelled = lambda: False
            self.finder.deadline = time.monotonic() + 20
            self.assertEqual(self.finder._aliases(track, "tr"), [track])
            self.assertEqual(self.finder._json.call_count, 2)
        album = dict(collectionId=50, collectionName="Album", artistName="Luna")
        for title, duration, collection in (("Moonlight (Instrumental)", 90000, 50),
                                            (track.title, 115000, 50), (track.title, 100000, 99)):
            recording = self.apple_record(42, title, "Luna", duration); recording["collectionId"] = collection
            self.finder._json = Mock(side_effect=[dict(results=[]), dict(results=[album]), dict(results=[recording])])
            self.assertEqual(self.finder._aliases(track, "tr"), [track])
            self.assertEqual(self.finder._json.call_count, 3)
        self.assertNotIn((track, "tr"), self.finder.alias_cache)

    def test_one_failed_album_lookup_does_not_hide_another_matching_album(self):
        track = Track("Moonlight", "Luna", "Album", 100)
        recording = self.apple_record(42, "Moonlight", "Luna", 100000); recording["collectionId"] = 51
        localized = self.apple_record(42, "달빛", "루나", 100000)
        self.finder._json = Mock(side_effect=[dict(results=[]), dict(results=[
            dict(collectionId=50, collectionName="Album", artistName="Luna"),
            dict(collectionId=51, collectionName="Album", artistName="Luna")]),
            requests.ConnectionError("temporary"), dict(results=[recording]), dict(results=[localized])])
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 20
        aliases = self.finder._aliases(track, "tr")
        self.assertEqual(aliases[1].title, "달빛")

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

    def test_cached_result_also_respects_cancellation(self):
        track = Track("月光", "歌手", duration=100)
        self.finder.cache[track, "tr", DEFAULTS["lyric_source_order"]] = Lyrics(parse_lrc(SAMPLE), "测试")
        with self.assertRaises(SearchCancelled):
            self.finder.find(track, cancelled=lambda: True)
