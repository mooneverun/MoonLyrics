import html
import time
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from moonlyrics.lyrics import LyricsFinder, Lyrics, LyricLine, Track, match_rank, SearchCancelled

LRC = "[00:01.20]月光落下\n[00:04.50]窗前等候"


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


class AnonymousSourceTests(TestCase):
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
        self.finder._json = Mock(side_effect=[dict(code=200, result=dict(songs=[self.netease_song()])),
                                             dict(code=200, lrc=dict(lyric="只有纯文本"))])
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
        self.finder._json = Mock(side_effect=[search, dict(code=0, lyric="只有纯文本")])
        self.assertIsNone(self.finder._qq([self.track]))

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
