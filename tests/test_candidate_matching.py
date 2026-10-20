"""Exercise candidate recovery through the same entry point as automatic matching."""
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from moonlyrics.lyrics import LyricsFinder, Track, LyricCandidate, metadata_rank, match_rank

LRC = "[00:01]First synthetic cue\n[00:03]Second synthetic cue"


class CandidateMatchingTests(TestCase):
    def setUp(self):
        self.finder = LyricsFinder()
        self.addCleanup(self.finder.close)
        self.target = Track("月光", "Luna", "月光 - Single", 100)
        self.original = dict(kind="song", trackId=42, collectionId=50, artistId=60,
                             trackName="月光", artistName="Luna", collectionName="月光 - Single",
                             trackTimeMillis=100000, trackCount=1, trackNumber=1, discNumber=1,
                             releaseDate="2024-01-02T12:00:00Z", artworkUrl100="apple-cover")
        self.song = dict(id=21, name="月光 Moonlight", duration=100001, no=1,
                         artists=[dict(id=12, name="Luna")], album=dict(id=30, name="月光 Moonlight"))
        self.album = dict(id=30, name="月光 Moonlight", publishTime=1704153600000,
                          size=1, picUrl="source-cover")
        self.finder._same_cover = Mock(return_value=True)
        for source in ("qq", "lrclib", "kugou"):
            setattr(self.finder, "_" + source, Mock(return_value=None))

    def response(self, url, **params):
        if "itunes.apple.com/search" in url:
            return dict(results=[self.original])
        if "itunes.apple.com/lookup" in url:
            return dict(results=[self.original])
        if "search/get" in url:
            return dict(code=200, result=dict(songs=[self.song]))
        if "/api/album/" in url:
            return dict(code=200, album=dict(self.album, songs=[self.song]))
        if "song/lyric" in url:
            return dict(code=200, lrc=dict(lyric=LRC))
        raise AssertionError("Unexpected request: " + url)

    def find(self):
        self.finder._json = Mock(side_effect=self.response)
        return self.finder.find(self.target, source_order=("netease", "qq", "lrclib", "kugou"))

    def test_partial_bilingual_title_is_verified_with_album_full_artists_and_precise_timing(self):
        document, _ = self.find()
        self.assertIsNotNone(document)
        self.assertEqual(document.title, self.song["name"])
        self.finder._same_cover.assert_not_called()

    def test_missing_credit_needs_independent_single_album_evidence(self):
        self.song["artists"].append(dict(id=13, name="Guest"))
        document, _ = self.find()
        self.assertIsNotNone(document)
        self.finder._same_cover.assert_called_once()
        self.assertEqual(self.finder.diagnostics["outcome"], "matched")

    def test_missing_credit_is_not_accepted_when_cover_date_or_precision_disagrees(self):
        for kind in ("cover", "date", "duration", "wrong_artist", "version"):
            with self.subTest(kind=kind):
                self.setUp()
                self.song["artists"].append(dict(id=13, name="Guest"))
                if kind == "cover":
                    self.finder._same_cover.return_value = False
                elif kind == "date":
                    self.album["publishTime"] += 86400000
                elif kind == "duration":
                    self.song["duration"] += 800
                elif kind == "wrong_artist":
                    self.song["artists"][0]["name"] = "Stranger"
                else:
                    self.song["name"] += " (Instrumental)"
                self.assertIsNone(self.find()[0])

    def test_retained_candidate_is_rechecked_without_searching_again(self):
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 10
        self.finder._json = Mock(side_effect=self.response)
        self.song.update(name="달빛", album=dict(name="Album"))
        self.assertIsNone(self.finder._netease([Track("Moonlight", "Luna", "Album", 100)]))
        self.finder._json.reset_mock()
        document = self.finder._resolve_candidates([Track("달빛", "Luna", "Album", 100)], ("netease",))
        self.assertIsNotNone(document)
        self.assertTrue(all("song/lyric" in call.args[0] for call in self.finder._json.call_args_list))

    def test_structural_miss_does_not_request_an_identical_automatic_retry(self):
        self.song["name"] = "Completely unrelated"
        self.target = Track("Moonlight", "Different Artist", duration=100)
        self.finder._aliases = Mock(side_effect=lambda track, country: [track])
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        self.assertIsNone(self.find()[0])
        self.assertFalse(self.finder.retryable)

    def test_album_artist_catalog_recovers_a_song_missing_from_public_search(self):
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 10
        def response(url, **params):
            if "search" in url:
                return dict(results=[dict(artistId=60, artistName="Luna")]
                            if params.get("entity") == "musicArtist" else [])
            if params.get("id") == 60:
                return dict(results=[dict(collectionId=50, collectionName="月光 - Single", artistName="Luna")])
            return dict(results=[self.original])
        self.finder._json = Mock(side_effect=response)
        self.finder._artist_catalog(self.target, "tr")
        self.assertEqual(self.finder.apple_records[self.target, "tr"]["trackId"], 42)

    def test_competing_bilingual_titles_are_not_chosen_arbitrarily(self):
        other = dict(self.song, id=22, name="月光 Moonbeam")
        def response(url, **params):
            if "search/get" in url:
                return dict(code=200, result=dict(songs=[self.song, other]))
            return self.response(url, **params)
        self.finder._json = Mock(side_effect=response)
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 10
        self.assertIsNone(self.finder._netease([self.target]))
        self.assertFalse(any("song/lyric" in call.args[0] for call in self.finder._json.call_args_list))

    def test_version_word_is_not_treated_as_a_second_language_title(self):
        self.song["name"] = "月光 Live"
        self.assertIsNone(self.find()[0])

    def test_unlabelled_alternate_recording_is_retained_but_not_automatically_adopted(self):
        self.song.update(name=self.target.title, duration=102218)
        candidate = Track(self.target.title, "Luna", self.target.album, 102.218)
        self.assertIsNotNone(metadata_rank(candidate, [self.target]))
        self.assertIsNone(match_rank(candidate, [self.target]))
        document, _ = self.find()
        self.assertIsNone(document)
        self.assertIn(("netease", self.song["id"]), self.finder.candidates)
        self.assertFalse(any("song/lyric" in call.args[0] for call in self.finder._json.call_args_list))

    def test_qq_single_release_can_be_verified_when_other_catalog_is_unavailable(self):
        self.finder.current_track = self.target
        self.finder.current_storefront = "tr"
        self.finder.apple_records[self.target, "tr"] = self.original
        song = dict(mid="songmid", title="月光 Moonlight", interval=100,
                    singer=[dict(mid="leadmid", name="Luna"), dict(mid="guestmid", name="Guest")],
                    album=dict(id=30, mid="albummid", name="月光 Moonlight"))
        candidate = LyricCandidate("qq", "songmid", Track(song["title"], "Luna & Guest", song["album"]["name"], 100),
                                   tuple(song["singer"]), song)
        responses = [dict(code=0, req=dict(code=0, data=dict(basicInfo=dict(
            albumMid="albummid", albumType="Single", publishDate="2024-01-02")))),
            dict(code=0, req=dict(code=0, data=dict(albumMid="albummid", totalNum=1, songList=[dict(songInfo=song)])))]
        self.finder._json = Mock(side_effect=responses)
        self.assertTrue(self.finder._verify_single(candidate, [self.target]))
        self.assertIn("single_release", candidate.proof)

    def test_network_retry_reuses_successful_queries_but_retries_failed_requests(self):
        self.finder._lrclib = LyricsFinder._lrclib.__get__(self.finder)
        self.finder._aliases = Mock(side_effect=lambda track, country: [track])
        self.finder._album_aliases = Mock(side_effect=lambda track, country, aliases: aliases)
        self.finder._catalog_aliases = Mock(side_effect=lambda aliases: aliases)
        self.finder._netease = Mock(return_value=None)
        self.finder._kugou = Mock(return_value=None)
        self.finder._qq = Mock(return_value=None)
        outage = [True]
        def get(url, **params):
            if outage[0] and params["params"].get("artist_name"):
                from requests import ConnectionError
                raise ConnectionError("temporary")
            response = Mock(content=b"[]", status_code=200)
            response.json.return_value = []
            return response
        self.finder.session.get = Mock(side_effect=get)
        target = Track("Moonlight", "Luna", duration=100)
        self.finder.find(target, source_order=("lrclib", "netease", "qq", "kugou"))
        calls = self.finder.session.get.call_count
        outage[0] = False
        self.finder.find(target, source_order=("lrclib", "netease", "qq", "kugou"))
        self.assertEqual(self.finder.session.get.call_count - calls, 1)

    def test_logical_catalog_error_is_not_reused_as_a_successful_response(self):
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 10
        response = Mock(content=b"{}", status_code=200)
        response.json.side_effect = [dict(code=0, req=dict(code=106)), dict(code=0, req=dict(code=0, data={}))]
        self.finder.session.post = Mock(return_value=response)
        for _ in range(2):
            self.finder._json("https://u.y.qq.com/cgi-bin/musicu.fcg", body={"query": "same"})
        self.assertEqual(self.finder.session.post.call_count, 2)

    def test_rejected_combined_query_still_tries_a_title_only_query(self):
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 10
        target = Track("Moonlight", "Luna", duration=100)
        for provider in ("netease", "qq"):
            def response(url, **params):
                if "song/lyric" in url:
                    return dict(code=200, lrc=dict(lyric=LRC))
                if "search/get" in url:
                    return (dict(code=400) if params["s"] != target.title else dict(code=200, result=dict(songs=[
                        dict(id=21, name=target.title, duration=100000, artists=[dict(id=12, name="Luna")], album={})])))
                if "musicu.fcg" in url:
                    if params["body"]["req"]["param"]["query"] != target.title:
                        return dict(code=0, req=dict(code=2001))
                    return dict(code=0, req=dict(code=0, data=dict(body=dict(song=dict(list=[dict(
                        mid="songmid", title=target.title, interval=100, singer=[dict(mid="leadmid", name="Luna")], album={})])))))
                return dict(code=0, lyric=LRC)
            self.finder._json = Mock(side_effect=response)
            method = getattr(LyricsFinder, "_" + provider).__get__(self.finder)
            self.assertIsNotNone(method([target]), provider)

    def test_identical_flat_placeholder_covers_do_not_verify_a_release(self):
        from PyQt6.QtCore import QBuffer, QIODevice
        from PyQt6.QtGui import QImage, QColor
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor(20, 40, 100))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, "PNG"))
        response = Mock(content=bytes(buffer.data()), status_code=200)
        self.finder.session.get = Mock(return_value=response)
        self.finder.cancelled = lambda: False
        self.finder.deadline = time.monotonic() + 10
        with patch.object(self.finder, "_same_cover", wraps=LyricsFinder._same_cover.__get__(self.finder)):
            self.assertFalse(self.finder._same_cover("https://is1-ssl.mzstatic.com/a.jpg", "https://p1.music.126.net/a.jpg"))
