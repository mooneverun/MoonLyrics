import html
from itertools import permutations
import time
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from moonlyrics.lyrics import LyricsFinder, Lyrics, LyricLine, Track, match_rank, SearchCancelled
from moonlyrics.styles import LYRIC_SOURCES

LRC = "[00:01.20]月光落下\n[00:04.50]窗前等候"


class ArtistIdentityTests(TestCase):
    def test_incomplete_profile_does_not_poison_the_next_automatic_attempt(self):
        track = Track("Moonlight", "달게스트", duration=100)
        incomplete = dict(code=200, artist=dict(id=12, name="合作歌手", alias=[], transNames=[]))
        profiles = iter([incomplete, self.profile()])
        def response(url, **params):
            if "/api/artist/" in url:
                return next(profiles)
            if "song/lyric" in url:
                return dict(code=200, lrc=dict(lyric=LRC))
            return dict(code=200, result=dict(songs=[dict(id=21, name="Moonlight", duration=100000,
                artists=[dict(id=12, name="合作歌手")], album=dict(name="Album"))]))
        self.finder._json = Mock(side_effect=response)
        self.finder._aliases = Mock(return_value=[track]); self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        for source in ("qq", "lrclib", "kugou"):
            setattr(self.finder, "_" + source, Mock(return_value=None))
        first, _ = self.finder.find(track, source_order=("netease", "qq", "lrclib", "kugou"))
        second, _ = self.finder.find(track, source_order=("netease", "qq", "lrclib", "kugou"))
        self.assertIsNone(first)
        self.assertIsNotNone(second)

    def setUp(self):
        self.finder = LyricsFinder()
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 20
        self.finder.reachable = False
        self.addCleanup(self.finder.close)
        self.aliases = [Track("Moonlight (feat. Star Guest)", "Lead Name", "Album", 100),
                        Track("달빛 (feat. 달게스트)", "한국리드", "Album", 100)]

    def song(self, artists=None):
        return dict(id=21, name="달빛", artists=artists or [dict(id=11, name="Lead Name"), dict(id=12, name="合作歌手")],
                    album=dict(name="Album"), duration=100000)

    def profile(self, identifier=12, name="合作歌手", **fields):
        return dict(code=200, artist=dict(id=identifier, name=name, trans="달게스트", transNames=["달게스트"], alias=[], **fields))

    def test_netease_uses_guest_profile_and_mixes_verified_recording_languages(self):
        song = self.song()
        def response(url, **params):
            if url.endswith("/12"):
                return self.profile()
            if "search/get" in url:
                return dict(code=200, result=dict(songs=[song]))
            if "song/lyric" in url:
                return dict(code=200, lrc=dict(lyric=LRC))
            raise AssertionError("Unnecessary or unexpected profile request")
        self.finder._json = Mock(side_effect=response)
        document = self.finder._netease(self.aliases)
        self.assertIsNotNone(document)
        self.assertEqual(document.source, "网易云音乐")
        self.assertEqual(sum(call.args[0].endswith("/12") for call in self.finder._json.call_args_list), 1)

    def test_qq_uses_matching_artist_mid_and_structured_foreign_name(self):
        song = dict(mid="songmid", name="달빛", singer=[dict(mid="leadmid", name="Lead Name"),
            dict(mid="guestmid", name="合作歌手")], album=dict(name="Album"), interval=100)
        def response(url, **params):
            if "musicu.fcg" in url:
                request = params["body"]["req"]
                if request["method"] == "GetSingerDetail":
                    self.assertEqual(request["param"]["singer_mids"], ["guestmid"])
                    return dict(code=0, req=dict(code=0, data=dict(singer_list=[dict(
                        basic_info=dict(singer_mid="guestmid", name="合作歌手 (달게스트)"),
                        ex_info=dict(foreign_name="달게스트", desc="irrelevant text"))])))
                return dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[song])))))
            return dict(code=0, lyric=LRC)
        self.finder._json = Mock(side_effect=response)
        document = self.finder._qq(self.aliases)
        self.assertIsNotNone(document)
        self.assertEqual(document.source, "QQ音乐")

    def test_qq_resolves_localized_feature_with_affiliation_against_source_identities(self):
        song = dict(mid="songmid", name="달빛 (Moonlight) (feat. 달게스트 of Group)",
                    singer=[dict(mid="leadmid", name="Lead Name"), dict(mid="guestmid", name="Star Guest")],
                    album=dict(name="Album"), interval=100)
        def response(url, **params):
            if "musicu.fcg" in url:
                request = params["body"]["req"]
                if request["method"] == "GetSingerDetail":
                    self.assertEqual(request["param"]["singer_mids"], ["guestmid"])
                    return dict(code=0, req=dict(code=0, data=dict(singer_list=[dict(
                        basic_info=dict(singer_mid="guestmid", name="Star Guest (달게스트)"),
                        ex_info=dict(foreign_name="달게스트"))])))
                return dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[song])))))
            return dict(code=0, lyric=LRC)
        self.finder._json = Mock(side_effect=response)
        document = self.finder._qq(self.aliases)
        self.assertIsNotNone(document)
        self.assertEqual(document.source, "QQ音乐")

    def test_wrong_profile_identity_or_name_is_not_used_as_an_alias(self):
        for invalid in (self.profile(identifier=99), self.profile(name="另一位歌手"),
                        dict(code=200, artist=None), dict(code=200, artist=dict(id=True, name="合作歌手", trans="달게스트"))):
            self.finder._json = Mock(return_value=invalid)
            self.assertIsNone(self.finder._artist_profile("netease", dict(id=12, name="合作歌手")))

    def test_affiliations_cannot_hide_unknown_guests_or_shorten_primary_artists(self):
        self.finder._artist_profile = Mock(return_value=frozenset())
        for title in ("Moonlight (feat. Stranger of Group)",
                      "Moonlight (feat. Star Guest of Group & Stranger)"):
            candidate = Track(title, "Lead Name & Star Guest", "Album", 100)
            records = [dict(mid="leadmid", name="Lead Name"), dict(mid="guestmid", name="Star Guest")]
            self.assertEqual(list(self.finder._source_matches("qq", [((False, 0), "songmid", candidate, records)], self.aliases)), [])
        candidate = Track("Moonlight", "Boards of Canada", duration=100)
        records = [dict(mid="bandmid", name="Boards of Canada")]
        self.assertEqual(list(self.finder._source_matches("qq", [((False, 0), "songmid", candidate, records)],
                                                         [Track("Moonlight", "Boards", duration=100)])), [])

    def test_positive_profiles_are_cached_and_failed_lookups_can_recover(self):
        self.finder._json = Mock(side_effect=[requests.ConnectionError("temporary"), self.profile()])
        record = dict(id=12, name="合作歌手")
        self.assertIsNone(self.finder._artist_profile("netease", record))
        names = self.finder._artist_profile("netease", record)
        self.assertTrue(names)
        self.assertEqual(self.finder._artist_profile("netease", record), names)
        self.assertEqual(self.finder._json.call_count, 2)

    def test_a_new_catalogue_name_for_a_cached_id_is_revalidated(self):
        self.finder._json = Mock(side_effect=[self.profile(), self.profile(name="New Name")])
        self.finder._artist_profile("netease", dict(id=12, name="合作歌手"))
        self.assertTrue(self.finder._artist_profile("netease", dict(id=12, name="New Name")))
        self.assertEqual(self.finder._json.call_count, 2)

    def test_missing_or_extra_people_and_distinct_ids_with_the_same_name_are_rejected(self):
        for artists in ([dict(id=12, name="合作歌手")],
                        [dict(id=11, name="Lead Name"), dict(id=12, name="合作歌手"), dict(id=13, name="Extra")],
                        [dict(id=11, name="Lead Name"), dict(id=12, name="Lead Name")]):
            self.finder._json = Mock(return_value=dict(code=200, result=dict(songs=[self.song(artists)])))
            self.assertIsNone(self.finder._netease(self.aliases))
            self.assertTrue(all("search/get" in call.args[0] for call in self.finder._json.call_args_list))

    def test_wrong_versions_and_durations_do_not_trigger_artist_requests(self):
        for title, duration in (("달빛 (Live)", 100000), ("달빛", 115000)):
            song = self.song(); song.update(name=title, duration=duration)
            self.finder._json = Mock(return_value=dict(code=200, result=dict(songs=[song])))
            self.assertIsNone(self.finder._netease(self.aliases))
            self.assertTrue(all("search/get" in call.args[0] for call in self.finder._json.call_args_list))

    def test_cached_identity_still_obeys_cancellation(self):
        self.finder._json = Mock(return_value=self.profile())
        record = dict(id=12, name="合作歌手")
        self.finder._artist_profile("netease", record)
        self.finder.cancelled = lambda: True
        with self.assertRaises(SearchCancelled):
            self.finder._artist_profile("netease", record)

    def test_wrong_qq_mid_or_numeric_id_does_not_supply_aliases(self):
        record = dict(id=12, mid="guestmid", name="合作歌手")
        for basic in (dict(singer_mid="anothermid", singer_id=12, name="合作歌手"),
                      dict(singer_mid="guestmid", singer_id=99, name="合作歌手"),
                      dict(singer_mid="guestmid", singer_id=True, name="合作歌手")):
            self.finder._json = Mock(return_value=dict(code=0, req=dict(code=0, data=dict(singer_list=[dict(
                basic_info=basic, ex_info=dict(foreign_name="달게스트"))]))))
            self.assertIsNone(self.finder._artist_profile("qq", record))

    def test_profile_cache_follows_the_artist_across_songs_and_is_source_scoped(self):
        self.finder._json = Mock(return_value=self.profile())
        self.finder._artist_profile("netease", dict(id=12, name="合作歌手"))
        for title in ("Moonlight", "Another Song"):
            aliases = [Track(title + " (feat. 달게스트)", "Lead Name", duration=100)]
            candidate = Track(title, "Lead Name & 合作歌手", duration=100)
            matches = [( (False, 0), 21, candidate, self.song()["artists"] )]
            self.assertEqual(len(list(self.finder._source_matches("netease", matches, aliases))), 1)
        self.finder._json.assert_called_once()
        self.finder._json = Mock(return_value=dict(code=0, req=dict(code=0, data=dict(singer_list=[dict(
            basic_info=dict(singer_mid="12", name="合作歌手"), ex_info=dict(foreign_name="Other Person"))]))))
        names = self.finder._artist_profile("qq", dict(mid="12", name="合作歌手"))
        self.assertNotEqual(names, self.finder.artist_cache[("netease", 12)])
        self.finder._json.assert_called_once()

    def test_duplicate_source_id_cannot_be_counted_as_two_performers(self):
        aliases = [Track("Moonlight (feat. Guest)", "Lead", duration=100)]
        candidate = Track("Moonlight", "Lead & Guest", duration=100)
        records = [dict(id=11, name="Lead"), dict(id=11, name="Guest")]
        self.finder._json = Mock()
        self.assertEqual(list(self.finder._source_matches("netease", [((False, 0), 21, candidate, records)], aliases)), [])
        self.finder._json.assert_not_called()

    def test_profile_requests_are_bounded_without_blocking_the_next_search(self):
        self.finder._json = Mock(side_effect=requests.ConnectionError("temporary"))
        for identifier in range(1, 10):
            self.finder._artist_profile("netease", dict(id=identifier, name="合作歌手"))
        self.assertEqual(self.finder._json.call_count, 6)
        with self.assertRaises(SearchCancelled):
            self.finder.find(Track("Moonlight", "Lead"), cancelled=lambda: True)
        self.assertEqual(self.finder.profile_requests, 0)

    def test_later_slow_profile_is_not_requested_before_a_valid_lyric_download(self):
        first = self.song([dict(id=11, name="Lead Name"), dict(id=12, name="달게스트")])
        second = self.song(); second["id"] = 22
        self.finder._json = Mock(side_effect=[dict(code=200, result=dict(songs=[first, second])),
                                             dict(code=200, lrc=dict(lyric=LRC))])
        self.assertIsNotNone(self.finder._netease(self.aliases))
        self.assertEqual(self.finder._json.call_count, 2)

    def test_unverified_candidates_do_not_crowd_out_a_known_performer(self):
        wrong = [self.song([dict(id=100 + index, name="Unknown"), dict(id=12, name="合作歌手")]) for index in range(8)]
        known = self.song([dict(id=11, name="Lead Name"), dict(id=12, name="달게스트")]); known["id"] = 99
        self.finder._json = Mock(side_effect=[dict(code=200, result=dict(songs=wrong + [known])),
                                             dict(code=200, lrc=dict(lyric=LRC))])
        self.assertIsNotNone(self.finder._netease(self.aliases))
        self.assertEqual(self.finder._json.call_args.kwargs["id"], 99)
        self.assertEqual(self.finder._json.call_count, 2)


class SourcePriorityTests(TestCase):
    def setUp(self):
        self.finder = LyricsFinder()
        self.addCleanup(self.finder.close)
        self.track = Track("Moonlight", "Luna", duration=100)
        self.finder._aliases = Mock(return_value=[self.track])
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)

    def test_every_priority_order_falls_back_in_the_chosen_sequence(self):
        for order in permutations(LYRIC_SOURCES):
            calls = []
            def search(source):
                def run(*args, **kwargs):
                    calls.append(source)
                    return Lyrics((LyricLine(1, "月光"),), source) if source == order[-1] else None
                return run
            for source in LYRIC_SOURCES:
                setattr(self.finder, "_" + source, Mock(side_effect=search(source)))
            document, _ = self.finder.find(self.track, source_order=order)
            self.assertIsNotNone(document)
            self.assertEqual(calls, list(order))

    def test_priority_changes_do_not_reuse_another_orders_cached_lyrics(self):
        for source in LYRIC_SOURCES:
            setattr(self.finder, "_" + source, Mock(return_value=Lyrics((LyricLine(1, "月光"),), source)))
        first, _ = self.finder.find(self.track)
        second, _ = self.finder.find(self.track, source_order=("qq", "netease", "kugou", "lrclib"))
        again, _ = self.finder.find(self.track)
        self.assertEqual((first.source, second.source, again.source), ("kugou", "qq", "kugou"))
        self.finder._kugou.assert_called_once()
        self.finder._qq.assert_called_once()

    def test_first_source_is_rechecked_with_verified_names_before_fallback(self):
        for source in LYRIC_SOURCES:
            setattr(self.finder, "_" + source, Mock(return_value=None))
        document = Lyrics((LyricLine(1, "月光"),), "QQ音乐")
        self.finder._aliases.return_value = [self.track, Track("달빛", "루나", duration=100)]
        self.finder._qq.side_effect = lambda aliases, **kwargs: document if len(aliases) > 1 else None
        result, _ = self.finder.find(self.track, source_order=("qq", "netease", "kugou", "lrclib"))
        self.assertIs(result, document)
        self.assertEqual(self.finder._qq.call_count, 2)
        for source in ("netease", "kugou", "lrclib"):
            getattr(self.finder, "_" + source).assert_not_called()


class RegionalMatchingTests(TestCase):
    def setUp(self):
        self.aliases = [Track("Moonlight (feat. Guest A & Guest B)", "Luna", "Album", 200),
                        Track("달빛 (feat. Guest A & Guest B)", "루나", "Album", 200)]

    def test_verified_titles_and_artists_can_use_different_languages(self):
        candidate = Track(self.aliases[0].title, "루나", "Album", 200)
        self.assertIsNotNone(match_rank(candidate, self.aliases))

    def test_bilingual_title_and_relocated_featured_artists_match(self):
        for candidate in [Track("달빛 (Moonlight)", "Guest B、Luna、Guest A", "Album", 200),
                          Track("달빛 (Moonlight) (feat. Guest A & Guest B)", "Luna (루나)", "Album", 200)]:
            self.assertIsNotNone(match_rank(candidate, self.aliases))

    def test_title_normalization_does_not_discard_guests_or_recording_versions(self):
        for candidate in [Track("달빛 (Moonlight)", "Luna", "Album", 200),
                          Track("달빛 (Moonlight)", "Luna & Guest A & Stranger", "Album", 200),
                          Track("달빛 (Moonlight) (Live)", "Luna & Guest A & Guest B", "Album", 200),
                          Track("달빛 (Other Song)", "Luna & Guest A & Guest B", "Album", 200),
                          Track("달빛 (Moonlight)", "Luna & Guest A & Guest B", "Album", 210)]:
            self.assertIsNone(match_rank(candidate, self.aliases))

    def test_korean_names_are_not_guessed_without_a_verified_alias(self):
        candidate = Track(self.aliases[0].title, "루나", "Album", 200)
        self.assertIsNone(match_rank(candidate, self.aliases[:1]))

    def test_locale_with_missing_guest_does_not_weaken_the_original_roster(self):
        incomplete = [self.aliases[0], Track("달빛", "루나", "Album", 200)]
        self.assertIsNone(match_rank(Track("달빛", "루나", "Album", 200), incomplete))
        self.assertIsNotNone(match_rank(Track("달빛", "루나 & Guest A & Guest B", "Album", 200), incomplete))


class AnonymousSourceTests(TestCase):
    def test_first_automatic_search_accepts_an_embedded_title_reading_omitted_by_the_source(self):
        track = Track("飞(FEY)1999", "99 God", "飞(FEY)1999", 98)
        def response(url, **params):
            if "search/get" in url:
                return dict(code=200, result=dict(songs=[dict(id=21, name="飞1999", duration=98950,
                    artists=[dict(id=12, name="99 God")], album=dict(name="飞1999"))]))
            if "song/lyric" in url:
                return dict(code=200, lrc=dict(lyric=LRC))
            raise AssertionError("An exact performer must not require alias requests")
        self.finder._json = Mock(side_effect=response)
        self.finder._aliases = Mock(return_value=[track])
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        for source in ("qq", "lrclib", "kugou"):
            setattr(self.finder, "_" + source, Mock(return_value=None))
        document, _ = self.finder.find(track, source_order=("netease", "qq", "lrclib", "kugou"))
        self.assertIsNotNone(document, "A real timed lyric candidate was discarded before download")
        self.assertEqual(document.source, "网易云音乐")
        self.assertEqual(document.title, "飞1999")
        self.finder._aliases.assert_not_called()

    def test_title_only_fallback_uses_artist_profiles_and_keeps_the_roster_check(self):
        track = Track("Moonlight", "Luna", duration=100)
        for source in ("netease", "qq"):
            def response(url, **params):
                if "/artist/" in url:
                    return dict(code=200, artist=dict(id=12, name="露娜", alias=["Luna"]))
                if "song/lyric" in url:
                    return dict(code=200, lrc=dict(lyric=LRC))
                if "search/get" in url:
                    return dict(code=200, result=dict(songs=[dict(id=21, name="Moonlight", duration=100000,
                        artists=[dict(id=12, name="露娜")], album=dict(name="Album"))] if params["s"] == "Moonlight" else []))
                if "musicu.fcg" in url:
                    request = params["body"]["req"]
                    if request["method"] == "GetSingerDetail":
                        return {"code": 0, "req": {"code": 0, "data": {"singer_list": [dict(
                            basic_info=dict(singer_mid="guestmid", name="Luna (露娜)"), ex_info={})]}}}
                    songs = [dict(mid="songmid", name="Moonlight", interval=100, album=dict(name="Album"),
                                  singer=[dict(mid="guestmid", name="露娜")])] if request["param"]["query"] == "Moonlight" else []
                    return {"code": 0, "req": {"code": 0, "data": {"body": {"song": {"list": songs}}}}}
                return dict(code=0, lyric=LRC)
            self.finder._json = Mock(side_effect=response)
            self.assertIsNotNone(getattr(self.finder, "_" + source)([track]))

    def setUp(self):
        self.finder = LyricsFinder()
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 20
        self.finder.reachable = False
        self.addCleanup(self.finder.close)
        self.track = Track("月光", "歌手", "Album", 100)

    def netease_song(self, identifier=1, title="月光", artist="歌手", duration=100000):
        return dict(id=identifier, name=title, artists=[dict(name=artist)],
                    album=dict(name="Album"), duration=duration)

    def qq_song(self, mid="testmid", title="月光", artist="歌手", duration=100):
        return dict(mid=mid, name=title, singer=[dict(name=artist)],
                    album=dict(name="Album"), interval=duration)

    def test_netease_checks_song_identity_before_fetching_lrc(self):
        self.finder._json = Mock(side_effect=[dict(code=200, result=dict(songs=[
            self.netease_song(2, artist="翻唱歌手"), self.netease_song()])),
            dict(code=200, lrc=dict(lyric=LRC))])
        document = self.finder._netease([self.track])
        self.assertEqual(document.source, "网易云音乐")
        self.assertEqual(len(document.lines), 2)
        self.assertEqual(self.finder._json.call_args.kwargs["id"], 1)

    def test_netease_blocked_search_is_recorded_without_trying_to_log_in(self):
        self.finder._json = Mock(return_value=dict(code=-462, message="blocked"))
        self.assertIsNone(self.finder._netease([self.track]))
        self.assertTrue(self.finder.failures)
        self.finder._json.assert_called_once()

    def test_netease_bad_download_does_not_hide_the_next_candidate(self):
        self.finder._json = Mock(side_effect=[dict(code=200, result=dict(songs=[
            self.netease_song(1), self.netease_song(2)])), requests.ConnectionError("temporary"),
            dict(code=200, lrc=dict(lyric=LRC))])
        self.assertIsNotNone(self.finder._netease([self.track]))
        self.assertEqual(self.finder._json.call_args.kwargs["id"], 2)

    def test_netease_plain_text_is_not_treated_as_synced_lyrics(self):
        self.finder._json = Mock(side_effect=lambda url, **params:
            dict(code=200, result=dict(songs=[self.netease_song()])) if "search/get" in url
            else dict(code=200, lrc=dict(lyric="只有纯文本")))
        self.assertIsNone(self.finder._netease([self.track]))

    def test_qq_anonymous_search_and_html_encoded_lrc(self):
        body = dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[
            self.qq_song(artist="翻唱歌手"), self.qq_song()])))))
        self.finder._json = Mock(side_effect=[body, dict(code=0, lyric=html.escape(LRC).replace(":", "&#58;"))])
        document = self.finder._qq([self.track])
        self.assertEqual(document.source, "QQ音乐")
        self.assertEqual(len(document.lines), 2)
        self.assertEqual(self.finder._json.call_args.kwargs["songmid"], "testmid")
        self.assertEqual(self.finder._json.call_args.kwargs["nobase64"], 1)
        search = self.finder._json.call_args_list[0].kwargs["body"]
        self.assertEqual(search["comm"]["uin"], "0")
        self.assertEqual(search["req"]["param"]["query"], "歌手 月光")

    def test_qq_bad_download_does_not_hide_the_next_candidate(self):
        body = dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[
            self.qq_song("first"), self.qq_song("second")])))))
        self.finder._json = Mock(side_effect=[body, requests.ConnectionError("temporary"), dict(code=0, lyric=LRC)])
        self.assertIsNotNone(self.finder._qq([self.track]))
        self.assertEqual(self.finder._json.call_args.kwargs["songmid"], "second")

    def test_qq_service_error_does_not_become_an_empty_catalogue(self):
        self.finder._json = Mock(return_value=dict(code=0, req=dict(code=2000)))
        self.assertIsNone(self.finder._qq([self.track]))
        self.assertTrue(self.finder.failures)

    def test_incomplete_artist_lists_are_rejected_before_downloading(self):
        for provider, make_song, field in [(self.finder._netease, self.netease_song, "artists"),
                                          (self.finder._qq, self.qq_song, "singer")]:
            bad, good = make_song(), make_song()
            bad[field].append(None)
            if field == "artists":
                bad["id"], good["id"] = 1, 2
                search = dict(code=200, result=dict(songs=[bad, good]))
                lyric = dict(code=200, lrc=dict(lyric=LRC))
            else:
                bad["mid"], good["mid"] = "bad", "good"
                search = dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[bad, good])))))
                lyric = dict(code=0, lyric=LRC)
            self.finder._json = Mock(side_effect=[search, lyric])
            self.assertIsNotNone(provider([self.track]))
            args = self.finder._json.call_args.kwargs
            self.assertEqual(args.get("id") if field == "artists" else args.get("songmid"), 2 if field == "artists" else "good")

    def test_new_sources_propagate_cancellation_without_further_requests(self):
        for provider in (self.finder._netease, self.finder._qq):
            self.finder._json = Mock(side_effect=SearchCancelled())
            with self.assertRaises(SearchCancelled):
                provider([self.track])
            self.finder._json.assert_called_once()

    def test_qq_plain_text_is_not_treated_as_synced_lyrics(self):
        search = dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[self.qq_song()])))))
        self.finder._json = Mock(side_effect=lambda url, **params: search if "musicu.fcg" in url else dict(code=0, lyric="只有纯文本"))
        self.assertIsNone(self.finder._qq([self.track]))

    def test_timestamped_placeholders_do_not_hide_the_next_download(self):
        for source in ("netease", "qq"):
            if source == "netease":
                search = dict(code=200, result=dict(songs=[self.netease_song(1), self.netease_song(2)]))
                bad = dict(code=200, lrc=dict(lyric="[00:00.00]暂无歌词"))
                good = dict(code=200, lrc=dict(lyric=LRC))
            else:
                search = dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[
                    self.qq_song("first"), self.qq_song("second")])))))
                bad = dict(code=0, lyric="[00:00.00]暂无歌词"); good = dict(code=0, lyric=LRC)
            self.finder._json = Mock(side_effect=[search, bad, good])
            self.assertEqual(len(getattr(self.finder, "_" + source)([self.track]).lines), 2)
            params = self.finder._json.call_args.kwargs
            self.assertEqual(params.get("id") if source == "netease" else params.get("songmid"), 2 if source == "netease" else "second")

    def test_exact_album_is_preferred_in_new_source_candidates(self):
        other, exact = self.netease_song(1), self.netease_song(2)
        other["album"]["name"] = "Other Album"
        self.finder._json = Mock(side_effect=[dict(code=200, result=dict(songs=[other, exact])),
                                             dict(code=200, lrc=dict(lyric=LRC))])
        self.assertIsNotNone(self.finder._netease([self.track]))
        self.assertEqual(self.finder._json.call_args.kwargs["id"], 2)

    def test_json_post_keeps_cancellation_and_bounded_timeouts(self):
        response = Mock(content=b"{}", status_code=200)
        response.json.return_value = dict(code=0)
        self.finder.session.post = Mock(return_value=response)
        self.finder._json("https://u.y.qq.com/cgi-bin/musicu.fcg", body=dict(comm=dict(uin="0")))
        request = self.finder.session.post.call_args.kwargs
        self.assertEqual(request["json"], dict(comm=dict(uin="0")))
        self.assertLessEqual(sum(request["timeout"]), 4.5)
        self.finder.cancelled = lambda: True
        with self.assertRaises(SearchCancelled):
            self.finder._json("https://u.y.qq.com/cgi-bin/musicu.fcg", body={})
        self.finder.session.post.assert_called_once()

    def test_slow_sources_still_leave_a_turn_for_qq(self):
        clock = [0.]
        document = Lyrics((LyricLine(1, "月光"),), "QQ音乐")
        def slow(*args, **kwargs):
            clock[0] = self.finder.stage_deadline + .01
            self.finder._check()
        self.finder._kugou = Mock(side_effect=slow)
        self.finder._lrclib = Mock(side_effect=slow)
        self.finder._netease = Mock(side_effect=slow)
        self.finder._qq = Mock(return_value=document)
        self.finder._aliases = Mock(return_value=[self.track])
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        with patch("moonlyrics.lyrics.time.monotonic", side_effect=lambda: clock[0]):
            result, _ = self.finder.find(self.track)
        self.assertIs(result, document)

    def test_korean_aliases_are_verified_by_the_same_apple_id(self):
        track = Track("Moonlight", "Luna", duration=100)
        record = lambda identifier, title, artist: dict(kind="song", trackId=identifier,
            trackName=title, artistName=artist, trackTimeMillis=100000)
        self.finder._json = Mock(side_effect=[dict(results=[record(12, "Moonlight", "Luna")]),
            dict(results=[record(99, "错误", "陌生人"), record(12, "달빛", "루나")])])
        self.finder._lrclib = Mock(side_effect=lambda aliases, **kwargs: Lyrics((LyricLine(1, "月光"),), "测试") if len(aliases) > 1 else None)
        self.finder._kugou = Mock(return_value=None)
        self.finder._netease = Mock(return_value=None)
        self.finder._qq = Mock(return_value=None)
        document, _ = self.finder.find(track)
        self.assertIsNotNone(document)
        self.assertEqual(self.finder._json.call_args.kwargs["country"], "kr")
