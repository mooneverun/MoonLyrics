"""Recording regressions use catalog metadata; lyric text below is synthetic."""
import copy
import json
from pathlib import Path
import time
from unittest import TestCase
from unittest.mock import Mock

import requests

from moonlyrics.lyrics import LyricsFinder, Track, split_credit, normalize, match_rank

LRC = "[00:01]Regression cue one\n[00:03]Regression cue two"


class CatalogRegressionTests(TestCase):
    def setUp(self):
        self.finder = LyricsFinder()
        self.addCleanup(self.finder.close)
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 20

    def netease(self, song, target, profile=None):
        def response(url, **params):
            if "/api/artist/" in url:
                return profile or dict(code=200, artist=song["artists"][0])
            if "song/lyric" in url:
                return dict(code=200, lrc=dict(lyric=LRC))
            return dict(code=200, result=dict(songs=[song]))
        self.finder._json = Mock(side_effect=response)
        return self.finder._netease([target])

    def test_why_uses_source_title_translation_and_artist_alias(self):
        song = dict(id=1818844747, name="为什么", transNames=["Why"], duration=209801,
                    artists=[dict(id=1132392, name="马思唯")], album=dict(name="黑马"))
        profile = dict(code=200, artist=dict(id=1132392, name="马思唯", alias=["Masiwei"]))
        self.assertIsNotNone(self.netease(song, Track("Why", "Masiwei", "Dark Horse", 209), profile))

    def test_colon_bilingual_title_uses_both_verified_names_and_preserves_recording_checks(self):
        aliases = [Track("Moonlight", "Luna & Guest", "Album", 100),
                   Track("月光", "Luna & Guest", "专辑", 100)]
        for title in ("月光:Moonlight", "月光：Moonlight", "Moonlight:月光"):
            with self.subTest(title=title):
                self.assertIsNotNone(match_rank(Track(title, "Luna & Guest", "Album", 100), aliases))
        self.assertIsNone(match_rank(Track("月光:Moonlight", "Luna & Guest", duration=100), aliases[:1]))
        for candidate in (Track("달빛:Moonlight", "Luna & Guest", duration=100),
                          Track("月光:Moonlight", "Luna", duration=100),
                          Track("月光:Moonlight", "Luna & Stranger", duration=100),
                          Track("月光:Moonlight", "Luna & Guest", duration=110),
                          Track("月光:Moonlight (Live)", "Luna & Guest", duration=100)):
            self.assertIsNone(match_rank(candidate, aliases))
        self.assertIsNone(match_rank(Track("Moon:Light", "Luna", duration=100),
                                    [Track("Moon", "Luna", duration=100), Track("Light", "Luna", duration=100)]))

    def test_eva_automatic_match_recovers_after_verified_apple_locale_lookup(self):
        target = Track("Eva&Lov3", "Capper & kkluv", "Gun-fire Angel", 183.621)
        original = dict(kind="song", trackId=1857700494, collectionId=1857699778,
                        trackName=target.title, artistName=target.artist, collectionName=target.album,
                        trackTimeMillis=183621)
        localized = dict(original, trackName="新世紀情歌戰士", collectionName="槍火天使")
        song = dict(id=3323826649, name="新世紀情歌戰士:Eva&Lov3", duration=183620,
                    artists=[dict(id=12193174, name="Capper"), dict(id=28642672, name="kkluv")],
                    album=dict(id=352728713, name="槍火天使 Gun-fire Angel"))
        def response(url, **params):
            if "itunes.apple.com/search" in url:
                return dict(results=[original])
            if "itunes.apple.com/lookup" in url:
                return dict(results=[localized])
            if "search/get" in url:
                return dict(code=200, result=dict(songs=[song]))
            if "song/lyric" in url:
                self.assertEqual(params["id"], song["id"])
                return dict(code=200, lrc=dict(lyric=LRC))
            raise AssertionError("No other lookup is needed for the verified bilingual title")
        self.finder._json = Mock(side_effect=response)
        for source in ("qq", "lrclib", "kugou"):
            setattr(self.finder, "_" + source, Mock(return_value=None))
        result, _ = self.finder.find(target, source_order=("netease", "kugou", "qq", "lrclib"))
        self.assertIsNotNone(result)
        self.assertEqual((result.source, result.title), ("网易云音乐", song["name"]))
        self.assertFalse(any("/api/album/" in call.args[0] for call in self.finder._json.call_args_list))

    def test_search_result_artist_aliases_are_bound_to_the_source_artist(self):
        song = dict(id=12, name="달빛", duration=100000, album=dict(name="Album"),
                    artists=[dict(id=13, name="合作歌手", alias=["Guest Name"])])
        self.assertIsNotNone(self.netease(song, Track("달빛", "Guest Name", duration=100)))
        self.assertFalse(any("/api/artist/" in call.args[0] for call in self.finder._json.call_args_list))

    def test_source_translation_cannot_hide_a_live_recording(self):
        song = dict(id=12, name="为什么 (Live)", transNames=["Why"], duration=209000,
                    artists=[dict(id=13, name="Masiwei")], album=dict(name="Live"))
        self.assertIsNone(self.netease(song, Track("Why", "Masiwei", duration=209)))

    def test_album_version_cannot_hide_an_instrumental_recording(self):
        song = dict(id=12, name="为什么", transNames=["Why"], duration=209000,
                    artists=[dict(id=13, name="Masiwei")], album=dict(name="Dark Horse (Instrumental)"))
        self.assertIsNone(self.netease(song, Track("Why", "Masiwei", "Dark Horse", 209)))

    def test_explicit_album_label_is_not_an_audio_version_and_live_translations_agree(self):
        song = dict(id=12, name="为什么", transNames=["Why"], duration=209000,
                    artists=[dict(id=13, name="Masiwei")], album=dict(name="Dark Horse (Explicit)"))
        self.assertIsNotNone(self.netease(song, Track("Why", "Masiwei", "Dark Horse", 209)))
        song.update(name="为什么 (现场)", transNames=["Why (Live)"])
        self.assertIsNotNone(self.netease(song, Track("Why (Live)", "Masiwei", "Dark Horse", 209)))

    def test_translated_title_verifies_the_complete_featured_block(self):
        song = dict(id=421124596, name="Wave (파도) (Feat. 라비 Of 빅스, 릴보이 Of 긱스)",
                    transNames=["波涛", "Wave (feat.Ravi of Vixx, Lil Boi of Geeks)"], duration=308089,
                    artists=[dict(id=861772, name="Microdot"),
                             dict(id=121758622, name="라비 of 빅스, 릴보이 of 긱스")], album=dict(name="+64"))
        self.assertIsNotNone(self.netease(song, Track("Wave (feat. RAVI & lIlBOI)", "Microdot", "+64", 308)))
        for guest in ("RAVI", "RAVI & Stranger", "RAVI & lIlBOI & Extra"):
            self.assertIsNone(self.netease(song, Track("Wave (feat. " + guest + ")", "Microdot", "+64", 308)))

    def test_plain_lyrics_are_reported_separately(self):
        target = Track("Moonlight", "Luna", duration=100)
        def response(url, **params):
            if "search/get" in url:
                return dict(code=200, result=dict(songs=[dict(id=12, name="Moonlight", duration=100000,
                    artists=[dict(id=13, name="Luna")], album=dict(name="Album"))]))
            return dict(code=200, lrc=dict(lyric="Plain words without timestamps"))
        self.finder._json = Mock(side_effect=response)
        self.finder._aliases = Mock(return_value=[target])
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        for source in ("qq", "lrclib", "kugou"):
            setattr(self.finder, "_" + source, Mock(return_value=None))
        result, message = self.finder.find(target, source_order=("netease", "qq", "lrclib", "kugou"))
        self.assertIsNone(result)
        self.assertIn("没有时间轴", message)

    def test_rate_limited_source_is_not_hammered_by_fallback_or_automatic_retry(self):
        response = requests.Response(); response.status_code = 429; response.headers["Retry-After"] = "120"
        self.finder._json = Mock(side_effect=requests.HTTPError(response=response))
        target = Track("Moonlight", "Luna", duration=100)
        self.assertIsNone(self.finder._lrclib([target]))
        self.assertIsNone(self.finder._lrclib([target]))
        self.assertEqual(self.finder._json.call_count, 1)


class AlbumRegressionTests(TestCase):
    def setUp(self):
        self.catalogs = json.loads((Path(__file__).parent / "fixtures" / "album_catalogs.json").read_text(encoding="utf-8"))
        self.finder = LyricsFinder()
        self.addCleanup(self.finder.close)
        for source in ("qq", "lrclib", "kugou"):
            setattr(self.finder, "_" + source, Mock(return_value=None))
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)

    def replay(self, catalog, index):
        apple, songs = catalog["apple"], catalog["songs"]
        original, localized = apple[index], songs[index]
        target = Track(original["trackName"], original["artistName"], original["collectionName"],
                       round(original["trackTimeMillis"] / 1000))
        def response(url, **params):
            if "itunes.apple.com/search" in url:
                return dict(results=[original])
            if "itunes.apple.com/lookup" in url:
                return dict(results=apple if params["id"] == catalog["apple_album_id"] else [original])
            if "/api/album/" in url:
                return dict(code=200, album=dict(id=catalog["source_album_id"], songs=songs))
            if "search/get" in url:
                query = normalize(params["s"])
                matches = [song for song in songs if normalize(split_credit(song["name"])[0]) == query]
                if normalize(split_credit(localized["name"])[0]) in query:
                    matches = [localized]
                if catalog.get("initial_candidate") and normalize(split_credit(original["trackName"])[0]) in query:
                    matches = [localized]
                return dict(code=200, result=dict(songs=matches))
            if "song/lyric" in url:
                self.assertEqual(params["id"], localized["id"])
                return dict(code=200, lrc=dict(lyric=LRC))
            raise AssertionError("Unexpected catalog request: " + url)
        self.finder._json = Mock(side_effect=response)
        return target, self.finder.find(target, source_order=("netease", "qq", "lrclib", "kugou"))

    def test_maria_and_soul_searching_use_whole_album_evidence(self):
        for catalog, index in zip(self.catalogs, (3, 25)):
            with self.subTest(album=catalog["apple_album_id"]):
                target, (result, message) = self.replay(catalog, index)
                self.assertIsNotNone(result, message)
                self.assertEqual(result.artist, " & ".join(artist["name"] for artist in catalog["songs"][index]["artists"]))
                self.assertIn((target, "tr"), self.finder.alias_cache)

    def test_verified_album_and_artist_identity_are_reused_for_another_song(self):
        catalog = self.catalogs[0]
        self.replay(catalog, 3)
        names = self.finder.artist_cache["netease", 12453904]
        self.assertIn(normalize("11 JIAYI"), names)
        _, (result, message) = self.replay(catalog, 1)
        self.assertIsNotNone(result, message)
        self.assertFalse(any("/api/album/" in call.args[0] for call in self.finder._json.call_args_list))

    def test_album_alignment_rejects_wrong_order_duration_versions_and_duplicate_ids(self):
        apple = self.catalogs[0]["apple"]
        for kind in ("order", "duration", "version", "duplicate", "count", "no_anchors", "invalid_id"):
            songs = copy.deepcopy(self.catalogs[0]["songs"])
            if kind == "order":
                songs[0], songs[1] = songs[1], songs[0]
            elif kind == "duration":
                songs[3]["duration"] += 6000
            elif kind == "version":
                songs[3]["name"] += " (Live)"
            elif kind == "duplicate":
                songs[3]["id"] = songs[2]["id"]
            elif kind == "count":
                songs.pop()
            elif kind == "no_anchors":
                for song in songs:
                    song["name"] = "其他标题"; song["transNames"] = []; song["alias"] = []
            else:
                songs[3]["id"] = True
            self.assertIsNone(self.finder._align_album(apple, songs), kind)

    def test_incomplete_collaborator_on_another_track_does_not_hide_maria(self):
        catalog = self.catalogs[0]
        pairs = self.finder._align_album(catalog["apple"], catalog["songs"])
        identifiers = {record["trackId"] for record, _, _ in pairs}
        self.assertIn(catalog["apple"][3]["trackId"], identifiers)
        # This catalog's GLORY entry omits the featured performer, so it must not become an alias.
        self.assertNotIn(catalog["apple"][6]["trackId"], identifiers)

    def test_conflicting_album_identities_are_not_cached(self):
        catalog = self.catalogs[0]
        target = Track("MARIA", "11 JIAYI", "SHEPHERD", 178)
        original = catalog["apple"][3]
        conflicting = copy.deepcopy(catalog["songs"])
        for song in conflicting:
            song["artists"][0].update(id=999, name="Another Performer")
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 20
        self.finder.apple_records[target, "tr"] = original
        def response(url, **params):
            if "itunes.apple.com" in url:
                return dict(results=catalog["apple"])
            if "search/get" in url:
                left = copy.deepcopy(catalog["songs"][4]); right = copy.deepcopy(left)
                right["album"]["id"] = 999
                return dict(code=200, result=dict(songs=[left, right]))
            identifier = params["id"]
            return dict(code=200, album=dict(id=identifier,
                songs=conflicting if identifier == 999 else catalog["songs"]))
        self.finder._json = Mock(side_effect=response)
        self.assertEqual(self.finder._album_aliases(target, "tr", [target]), [target])
        self.assertEqual(self.finder.album_cache, {})
        self.assertEqual(self.finder.artist_cache, {})

    def test_unseen_names_and_identifiers_use_the_same_album_rule(self):
        apple = copy.deepcopy(self.catalogs[0]["apple"])
        songs = copy.deepcopy(self.catalogs[0]["songs"])
        for index, (original, song) in enumerate(zip(apple, songs)):
            original.update(trackId=800000 + index, trackName="Unseen title " + str(index), artistName="New Artist")
            song.update(id=900000 + index, name="全新译名" + str(index), alias=[], transNames=[],
                        artists=[dict(id=700000, name="全新歌手")])
        for index in (1, 8):
            songs[index]["name"] = apple[index]["trackName"]
        pairs = self.finder._align_album(apple, songs)
        self.assertEqual(len(pairs), len(apple))
        self.assertEqual(pairs[3][1].title, "全新译名3")


class ReportedSongReplayTests(TestCase):
    def test_all_reported_recordings_with_captured_anonymous_catalog_responses(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "matching_cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(fixture["cases"]), 12)
        for case in fixture["cases"]:
            with self.subTest(title=case["input"]["title"]):
                finder = LyricsFinder()
                responses = {(request["url"], json.dumps(request["params"], sort_keys=True)): request
                             for request in case["requests"]}
                def replay(url, **params):
                    if "accesskey" in params:
                        params["accesskey"] = "fixture-access"
                    request = responses.get((url, json.dumps(params, sort_keys=True)))
                    if request is None or "error" in request:
                        raise requests.ConnectionError("Recorded catalog request unavailable")
                    return copy.deepcopy(request["response"])
                finder._json = Mock(side_effect=replay)
                values = {key: value for key, value in case["input"].items() if key != "title_aliases"}
                try:
                    document, message = finder.find(Track(**values), source_order=("netease", "qq", "lrclib", "kugou"))
                    self.assertEqual(document is not None, case["expected_found"], message)
                    if document is not None:
                        self.assertEqual(document.source, case["expected_source"])
                        self.assertEqual(document.title, case["expected_title"])
                finally:
                    finder.close()
