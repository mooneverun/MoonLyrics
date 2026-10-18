"""Synchronized lyric parsing and conservative cross-store track matching."""
import base64
from bisect import bisect_right
from dataclasses import dataclass
import html
import logging
import re
import time
import unicodedata

from opencc import OpenCC
import requests

from .playback import finite

LOG = logging.getLogger("moonlyrics.lyrics")
_simplified, _traditional = OpenCC("t2s"), OpenCC("s2t")
_stamp = re.compile(r"\[(\d{1,3}):([0-5]?\d)(?:[.:](\d{1,3}))?\]")
_word_stamp = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d+)?>")
_remaster = re.compile(r"\s*[\[(（][^\])）]*(?:remaster|重制|重製)[^\])）]*[\])）]|\s*[-–—]\s*(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?", re.I)


def normalize(text):
    value = _simplified.convert(unicodedata.normalize("NFKC", str(text or ""))).casefold()
    value = unicodedata.normalize("NFKD", value).replace("ı", "i")
    return "".join(c for c in value if c.isalnum() and not unicodedata.combining(c))


def title_name(text):
    return normalize(_remaster.sub("", str(text or "")))


def has_chinese(text):
    return any("\u3400" <= char <= "\u9fff" for char in text)


def has_korean(text):
    return any("\uac00" <= char <= "\ud7af" or "\u1100" <= char <= "\u11ff" for char in text)


def split_credit(text):
    text = _remaster.sub("", unicodedata.normalize("NFKC", str(text or ""))).strip()
    for pattern in (r"\s*[\[(]\s*(?:feat(?:uring)?|ft)\.?\s+([^\])]+)[\])]\s*$",
                    r"\s+(?:feat(?:uring)?|ft)\.?\s+(.+)$"):
        match = re.search(pattern, text, re.I)
        if match:
            return text[:match.start()].strip(), match[1].strip()
    return text, ""


def bilingual_parts(text):
    match = re.fullmatch(r"(.+?)\s*[\[(]([^\[\]()]+)[\])]", unicodedata.normalize("NFKC", text).strip())
    if match:
        left, right = match[1].strip(), match[2].strip()
        asian = lambda value: has_chinese(value) or has_korean(value)
        if (asian(left) != asian(right) and re.search(r"[A-Za-z]", right if asian(left) else left)
                and normalize(right) not in ("live", "remix", "cover", "instrumental", "karaoke")):
            return left, right
    return ()


def artist_parts(value):
    parts = []
    for part in re.split(r"\s*(?:[,&、，;；]|\b(?:feat(?:uring)?|ft)\.?\s+)\s*",
                         unicodedata.normalize("NFKC", value), flags=re.I):
        keys = {normalize(name) for name in (part, *bilingual_parts(part)) if normalize(name)}
        if keys and not any(keys & known for known in parts):
            parts.append(keys)
    return parts


def same_artists(left, right):
    left_parts, remaining = artist_parts(left), artist_parts(right)
    if not left_parts or len(left_parts) != len(remaining) or len(left_parts) > 16:
        return False
    for part in left_parts:
        matches = [index for index, other in enumerate(remaining) if part & other]
        if len(matches) != 1:
            return False
        remaining.pop(matches[0])
    return True


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str = ""
    duration: float = 0.


def related_artist_name(left, right):
    """A Chinese name plus a Latin stage name is a candidate, never proof by itself."""
    if re.search(r"[、,，&/+＋;；|]", str(left) + str(right)):
        return False
    left, right = normalize(left), normalize(right)
    chinese = lambda value: "".join(c for c in value if "\u3400" <= c <= "\u9fff")
    base = chinese(left)
    if left == right or len(base) < 2 or base != chinese(right) or base not in (left, right):
        return False
    if any(marker in left + right for marker in ("cover", "remix", "karaoke", "instrumental", "tribute", "翻唱", "粉丝", "原唱", "feat")):
        return False
    return bool(re.fullmatch(r"[a-z0-9\u3400-\u9fff]+", left) and re.fullmatch(r"[a-z0-9\u3400-\u9fff]+", right))


@dataclass(frozen=True)
class LyricLine:
    time: float
    text: str


@dataclass(frozen=True)
class Lyrics:
    lines: tuple
    source: str = ""
    title: str = ""
    artist: str = ""

    def index_at(self, position):
        return bisect_right(self.lines, position, key=lambda line: line.time) - 1

    def scroll_at(self, position):
        """Ease into the next line before its cue, arriving exactly on time."""
        index = self.index_at(position)
        if index >= len(self.lines) - 1:
            return float(index)
        cue = self.lines[index + 1].time
        span = min(.55, (cue - self.lines[index].time) * .6) if index >= 0 else .55
        if span <= 0:
            return float(index)
        fraction = max(0., min(1., (position - cue + span) / span))
        eased = fraction ** 3 * (10 + fraction * (-15 + 6 * fraction))
        return index + eased


def parse_lrc(text):
    if not isinstance(text, str) or len(text) > 1_000_000:
        return ()
    offset = re.search(r"\[offset:\s*([+-]?\d+)\s*\]", text, re.I)
    adjustment = int(offset[1]) / 1000 if offset else 0.
    groups = {}
    for raw in text.splitlines()[:8000]:
        stamps = list(_stamp.finditer(raw))
        if not stamps:
            continue
        content = _simplified.convert(_word_stamp.sub("", raw[stamps[-1].end():]).strip())
        for stamp in stamps:
            seconds = int(stamp[1]) * 60 + int(stamp[2]) + int((stamp[3] or "0").ljust(3, "0")) / 1000
            seconds = max(0., seconds - adjustment)
            if seconds > 8 * 3600:
                continue
            texts = groups.setdefault(seconds, [])
            if content and content not in texts:
                texts.append(content)
    lines = tuple(LyricLine(seconds, " / ".join(texts)) for seconds, texts in sorted(groups.items()))
    return lines if any(line.text for line in lines) else ()


def match_rank(candidate, aliases, tolerance=5):
    """Title AND artist must agree; duration rejects different recordings."""
    ranks = []
    if not candidate.title or not candidate.artist:
        return None
    title, guests = split_credit(candidate.title)
    known_titles = {title_name(split_credit(alias.title)[0]) for alias in aliases}
    if title_name(title) not in known_titles:
        bilingual = bilingual_parts(title)
        if not bilingual or not all(title_name(part) in known_titles for part in bilingual):
            return None
    for expected in aliases:
        expected_guests = split_credit(expected.title)[1]
        if not same_artists(" & ".join(filter(None, (candidate.artist, guests))),
                            " & ".join(filter(None, (expected.artist, expected_guests)))):
            continue
        difference = abs(candidate.duration - expected.duration)
        if expected.duration > 0 and (candidate.duration <= 0 or difference > tolerance):
            continue
        ranks.append((normalize(candidate.album) != normalize(expected.album), difference))
    return min(ranks) if ranks else None


class SearchCancelled(Exception):
    pass


class StageExpired(Exception):
    pass


class LyricsFinder:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "MoonLyrics/0.1 (https://github.com/mooneverun/MoonLyrics)"
        self.cache = {}
        self.alias_cache = {}
        self.stage_deadline = None
        self.failures = set()

    def close(self):
        self.session.close()

    def _check(self):
        if self.cancelled():
            raise SearchCancelled()
        now = time.monotonic()
        if now >= self.deadline:
            raise TimeoutError("歌词匹配超时，可以重新尝试")
        if self.stage_deadline is not None and now >= self.stage_deadline:
            raise StageExpired()

    def _run_stage(self, operation, *args, budget=4., **kwargs):
        previous = self.stage_deadline
        self.stage_deadline = min(self.deadline, time.monotonic() + budget)
        try:
            self._check()
            result = operation(*args, **kwargs)
            self._check()
            return result
        except StageExpired:
            name = getattr(operation, "__name__", type(operation).__name__)
            self.failures.add(name)
            LOG.info("Lyric stage timed out: stage=%s; continuing with other sources", name)
            return None
        finally:
            self.stage_deadline = previous

    def _json(self, url, *, body=None, headers=None, **params):
        self._check()
        response = None
        try:
            remaining = min(self.deadline, self.stage_deadline or self.deadline) - time.monotonic()
            if remaining <= .1:
                if self.deadline - time.monotonic() <= .1:
                    raise TimeoutError("歌词匹配超时，可以重新尝试")
                raise StageExpired()
            options = dict(timeout=(min(1.5, remaining * .35), min(3., remaining * .65)))
            if headers:
                options["headers"] = headers
            response = (self.session.get(url, params=params, **options) if body is None
                        else self.session.post(url, params=params, json=body, **options))
            response.raise_for_status()
            if len(response.content) > 3_000_000:
                raise ValueError("Lyrics response is too large")
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self.failures.add(url)
            LOG.info("Lyric request failed: endpoint=%s status=%s error=%s",
                     url, getattr(response, "status_code", None), type(exc).__name__)
            raise
        self._check()
        return data

    @staticmethod
    def _apple_tracks(payload):
        if not isinstance(payload, dict):
            return []
        return [(item.get("trackId"), Track(str(item.get("trackName") or ""),
                str(item.get("artistName") or ""), str(item.get("collectionName") or ""),
                finite(item.get("trackTimeMillis")) / 1000))
                for item in payload.get("results", []) if isinstance(item, dict) and item.get("kind") == "song"]

    def _aliases(self, track, storefront):
        key = track, storefront
        if key in self.alias_cache:
            return self.alias_cache[key]
        aliases = [track]
        try:
            payload = self._json("https://itunes.apple.com/search", term=f"{track.title} {track.artist}",
                                 country=storefront, entity="song", limit=25)
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                self.failures.add("Apple")
                return aliases
            matches = [(match_rank(candidate, [track]), identifier, candidate)
                       for identifier, candidate in self._apple_tracks(payload)]
            matches = [item for item in matches if item[0] is not None and item[1]]
            if matches:
                _, identifier, original = min(matches, key=lambda item: item[0])
                chinese = has_chinese(track.title + track.artist + track.album)
                for country in (("hk", "tw", "kr") if chinese else ("kr", "hk", "tw")):
                    try:
                        localized = self._json("https://itunes.apple.com/lookup", id=identifier, country=country, entity="song")
                        if not isinstance(localized, dict) or not isinstance(localized.get("results"), list):
                            self.failures.add("Apple localized names")
                            continue
                        for found_id, candidate in self._apple_tracks(localized):
                            if found_id != identifier or abs(candidate.duration - original.duration) > 1:
                                continue
                            if not any(normalize(candidate.title) == normalize(alias.title)
                                       and normalize(candidate.artist) == normalize(alias.artist) for alias in aliases):
                                aliases.append(candidate)
                    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
                        self.failures.add("Apple localized names")
                        LOG.info("Localized name lookup failed: country=%s error=%s", country, type(error).__name__)
                    if any(has_korean(alias.title + alias.artist)
                           or has_chinese(alias.title) and has_chinese(alias.artist) for alias in aliases):
                        break
        except (requests.RequestException, ValueError, TypeError, KeyError):
            LOG.debug("Apple localized name lookup unavailable", exc_info=True)
        self._check()
        # An unavailable or empty lookup must not become a sticky negative cache.
        if len(aliases) > 1:
            if len(self.alias_cache) >= 64:
                self.alias_cache.pop(next(iter(self.alias_cache)))
            self.alias_cache[key] = aliases
        return aliases

    def _catalog_aliases(self, aliases):
        expanded = list(aliases)
        artists = {normalize(track.artist): track.artist for track in aliases if has_chinese(track.artist)}
        for artist in list(artists.values())[:2]:
            try:
                data = self._json("https://songsearch.kugou.com/song_search_v2",
                                  keyword=_simplified.convert(artist), page=1, pagesize=30)
                if not isinstance(data, dict) or data.get("status") != 1 or not isinstance(data.get("data"), dict):
                    self.failures.add("catalog")
                    continue
                records = data["data"].get("lists")
                if not isinstance(records, list):
                    self.failures.add("catalog")
                    continue
                candidates = {}
                for item in records[:30]:
                    if not isinstance(item, dict) or not isinstance(item.get("SingerName"), str):
                        continue
                    name = html.unescape(re.sub(r"<[^>]+>", "", item["SingerName"])).strip()
                    identifiers = item.get("SingerId")
                    if (len(name) > 128 or not related_artist_name(artist, name)
                            or not isinstance(identifiers, list) or len(identifiers) != 1):
                        continue
                    identifier = identifiers[0]
                    if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier <= 0:
                        continue
                    key = normalize(name)
                    if key not in candidates:
                        candidates[key] = (name, set())
                    candidates[key][1].add(identifier)
                # Multiple names or performer IDs are ambiguous, even when their spelling is close.
                if len(candidates) != 1:
                    continue
                name, identifiers = next(iter(candidates.values()))
                if len(identifiers) != 1:
                    continue
                for track in aliases:
                    candidate = Track(track.title, name, track.album, track.duration)
                    if candidate not in expanded:
                        expanded.append(candidate)
            except (requests.RequestException, ValueError, TypeError, KeyError):
                LOG.debug("Artist catalog lookup unavailable", exc_info=True)
        return expanded

    def _kugou(self, aliases, tolerance=5):
        queries = list(dict.fromkeys(_simplified.convert(f"{t.artist} - {t.title}").strip() for t in reversed(aliases)))
        for query in queries[:2]:
            try:
                data = self._json("https://lyrics.kugou.com/search", ver=1, man="yes", client="pc", keyword=query,
                                  duration=round(aliases[0].duration * 1000))
                if not isinstance(data, dict) or data.get("status") != 200:
                    self.failures.add("KuGou")
                    continue
                self.reachable = True
                matches = []
                candidates = data.get("candidates")
                if not isinstance(candidates, list):
                    self.failures.add("KuGou")
                    continue
                for item in candidates[:50]:
                    if not isinstance(item, dict):
                        continue
                    candidate = Track(str(item.get("song") or ""), str(item.get("singer") or ""),
                                      duration=finite(item.get("duration")) / 1000)
                    rank = match_rank(candidate, aliases, tolerance)
                    if rank is not None and item.get("id") and item.get("accesskey"):
                        matches.append((rank, item, candidate))
                LOG.info("Lyric search: provider=KuGou candidates=%s accepted=%s", len(candidates), len(matches))
                for _, item, candidate in sorted(matches, key=lambda item: item[0])[:2]:
                    try:
                        payload = self._json("https://lyrics.kugou.com/download", ver=1, client="pc", id=item["id"],
                                             accesskey=item["accesskey"], fmt="lrc", charset="utf8")
                        if not isinstance(payload, dict):
                            self.failures.add("KuGou download")
                            continue
                        raw = base64.b64decode(payload.get("content", ""), validate=True).decode("utf-8-sig")
                        lines = parse_lrc(raw)
                        if lines:
                            return Lyrics(lines, "酷狗", candidate.title, candidate.artist)
                    except (requests.RequestException, ValueError, TypeError, KeyError, UnicodeError) as error:
                        self.failures.add("KuGou download")
                        LOG.info("Lyric candidate download failed: provider=KuGou error=%s", type(error).__name__)
            except (requests.RequestException, ValueError, TypeError, KeyError, UnicodeError):
                LOG.debug("KuGou lyrics unavailable", exc_info=True)
        return None

    def _lrclib(self, aliases, tolerance=5):
        queries = []
        for track in reversed(aliases):
            for title in (track.title, _simplified.convert(track.title), _traditional.convert(track.title)):
                if not has_chinese(title):
                    queries.append((title, track.artist))
                queries.append((title, None))
        for title, artist in list(dict.fromkeys(queries))[:4]:
            try:
                params = dict(track_name=title)
                if artist is not None:
                    params["artist_name"] = artist
                data = self._json("https://lrclib.net/api/search", **params)
                if not isinstance(data, list):
                    self.failures.add("LRCLIB")
                    continue
                self.reachable = True
                matches = []
                for item in data[:200]:
                    if not isinstance(item, dict) or not item.get("syncedLyrics"):
                        continue
                    candidate = Track(str(item.get("trackName") or ""), str(item.get("artistName") or ""),
                                      str(item.get("albumName") or ""), finite(item.get("duration")))
                    rank = match_rank(candidate, aliases, tolerance)
                    if rank is not None:
                        matches.append((rank, item, candidate))
                LOG.info("Lyric search: provider=LRCLIB candidates=%s accepted=%s", len(data), len(matches))
                for _, item, candidate in sorted(matches, key=lambda item: item[0]):
                    lines = parse_lrc(item["syncedLyrics"])
                    if lines:
                        return Lyrics(lines, "LRCLIB", candidate.title, candidate.artist)
            except (requests.RequestException, ValueError, TypeError, KeyError):
                LOG.debug("LRCLIB lyrics unavailable", exc_info=True)
        return None

    @staticmethod
    def _search_queries(aliases):
        return list(dict.fromkeys(_simplified.convert(f"{track.artist} {split_credit(track.title)[0]}").strip()
                                  for track in aliases))[:3]

    def _netease(self, aliases, tolerance=5):
        headers = {"Referer": "https://music.163.com/"}
        for query in self._search_queries(aliases):
            try:
                data = self._json("https://music.163.com/api/search/get", headers=headers, s=query, type=1, limit=20)
                result = data.get("result") if isinstance(data, dict) else None
                if not isinstance(data, dict) or data.get("code") != 200 or not isinstance(result, dict):
                    self.failures.add("网易云音乐")
                    LOG.info("Lyric service unavailable: provider=NetEase code=%s", data.get("code") if isinstance(data, dict) else None)
                    return None
                songs = result.get("songs", [])
                if not isinstance(songs, list):
                    self.failures.add("网易云音乐")
                    return None
                self.reachable = True
                matches = []
                for item in songs[:50]:
                    if not isinstance(item, dict) or not isinstance(item.get("artists"), list):
                        continue
                    names = [artist.get("name") for artist in item["artists"] if isinstance(artist, dict)]
                    album = item.get("album")
                    if (not names or len(names) != len(item["artists"])
                            or not all(isinstance(name, str) and name for name in names) or not isinstance(album, dict)):
                        continue
                    candidate = Track(str(item.get("name") or ""), " & ".join(names), str(album.get("name") or ""),
                                      finite(item.get("duration")) / 1000)
                    rank = match_rank(candidate, aliases, tolerance)
                    identifier = item.get("id")
                    if rank is not None and type(identifier) is int and 0 < identifier < 10 ** 15:
                        matches.append((rank, identifier, candidate))
                LOG.info("Lyric search: provider=NetEase candidates=%s accepted=%s", len(songs), len(matches))
                for _, identifier, candidate in sorted(matches, key=lambda item: item[0])[:2]:
                    try:
                        payload = self._json("https://music.163.com/api/song/lyric", headers=headers,
                                             id=identifier, lv=-1, kv=-1, tv=-1)
                        if not isinstance(payload, dict) or payload.get("code") != 200:
                            self.failures.add("网易云音乐歌词")
                            continue
                        lrc = payload.get("lrc")
                        lines = parse_lrc(html.unescape(lrc.get("lyric") or "")) if isinstance(lrc, dict) else ()
                        if lines:
                            return Lyrics(lines, "网易云音乐", candidate.title, candidate.artist)
                    except (requests.RequestException, ValueError, TypeError, KeyError):
                        self.failures.add("网易云音乐歌词")
            except (requests.RequestException, ValueError, TypeError, KeyError):
                self.failures.add("网易云音乐")
        return None

    def _qq(self, aliases, tolerance=5):
        headers = {"Referer": "https://y.qq.com/"}
        for query in self._search_queries(aliases):
            try:
                body = dict(comm=dict(ct="19", cv="1859", uin="0"), req=dict(method="DoSearchForQQMusicDesktop",
                    module="music.search.SearchCgiService", param=dict(grp=1, num_per_page=20, page_num=1, query=query, search_type=0)))
                data = self._json("https://u.y.qq.com/cgi-bin/musicu.fcg", headers=headers, body=body)
                request = data.get("req") if isinstance(data, dict) else None
                if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(request, dict) or request.get("code") != 0:
                    self.failures.add("QQ音乐")
                    LOG.info("Lyric service unavailable: provider=QQMusic code=%s", request.get("code") if isinstance(request, dict) else None)
                    return None
                container = request
                for key in ("data", "body", "song"):
                    container = container.get(key) if isinstance(container, dict) else None
                songs = container.get("list") if isinstance(container, dict) else None
                if not isinstance(songs, list):
                    self.failures.add("QQ音乐")
                    return None
                self.reachable = True
                matches = []
                for item in songs[:50]:
                    if not isinstance(item, dict) or not isinstance(item.get("singer"), list):
                        continue
                    names = [artist.get("name") for artist in item["singer"] if isinstance(artist, dict)]
                    album = item.get("album")
                    if (not names or len(names) != len(item["singer"])
                            or not all(isinstance(name, str) and name for name in names) or not isinstance(album, dict)):
                        continue
                    candidate = Track(str(item.get("name") or ""), " & ".join(names), str(album.get("name") or ""),
                                      finite(item.get("interval")))
                    rank = match_rank(candidate, aliases, tolerance)
                    mid = item.get("mid")
                    if rank is not None and isinstance(mid, str) and re.fullmatch(r"[A-Za-z0-9]{1,64}", mid):
                        matches.append((rank, mid, candidate))
                LOG.info("Lyric search: provider=QQMusic candidates=%s accepted=%s", len(songs), len(matches))
                for _, mid, candidate in sorted(matches, key=lambda item: item[0])[:2]:
                    try:
                        payload = self._json("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg", headers=headers,
                                             songmid=mid, format="json", nobase64=1)
                        if not isinstance(payload, dict) or payload.get("code") != 0:
                            self.failures.add("QQ音乐歌词")
                            continue
                        lines = parse_lrc(html.unescape(payload.get("lyric") or ""))
                        if lines:
                            return Lyrics(lines, "QQ音乐", candidate.title, candidate.artist)
                    except (requests.RequestException, ValueError, TypeError, KeyError):
                        self.failures.add("QQ音乐歌词")
            except (requests.RequestException, ValueError, TypeError, KeyError):
                self.failures.add("QQ音乐")
        return None

    def _search_sources(self, aliases, providers, tolerance=5, reserve=0.):
        for index, provider in enumerate(providers):
            remaining = self.deadline - time.monotonic() - reserve
            budget = min(4., max(.1, remaining / (len(providers) - index)))
            found = self._run_stage(provider, aliases, tolerance=tolerance, budget=budget)
            if found:
                return found
        return None

    def find(self, track, storefront="tr", cancelled=lambda: False, force=False):
        self.cancelled, self.deadline, self.reachable = cancelled, time.monotonic() + 20, False
        self.failures = set()
        self._check()
        key = track, storefront
        if not force and key in self.cache:
            cached = self.cache[key]
            return cached, f"已匹配 · {cached.source} · {len(cached.lines)} 行（缓存）"
        if force:
            self.alias_cache.pop(key, None)
        self.cache.pop(key, None)
        try:
            self._check()
            base_aliases = aliases = [track]
            preferred = (self._kugou, self._lrclib) if has_chinese(track.title + track.artist) else (self._lrclib, self._kugou)
            providers = (*preferred, self._netease, self._qq)
            result = self._run_stage(providers[0], aliases, budget=3.)
            if result is None:
                aliases = self._run_stage(self._aliases, track, storefront, budget=3.) or aliases
                result = self._search_sources(aliases, providers if aliases != base_aliases else providers[1:], reserve=2.)
            if result is None:
                expanded = self._run_stage(self._catalog_aliases, aliases, budget=2.) or aliases
                if expanded != aliases:
                    result = self._search_sources(expanded, providers, tolerance=2)
                    if result is not None:
                        self.alias_cache[key] = expanded
            self._check()
            if result:
                if len(self.cache) >= 64:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = result
                return result, f"已匹配 · {result.source} · {len(result.lines)} 行"
            if self.failures:
                return None, "部分曲目或歌词查询暂未完成，可重新匹配"
            return None, "未找到可靠的同步歌词" if self.reachable else "歌词服务暂时不可用，可点击重新匹配"
        except TimeoutError as exc:
            return None, str(exc)


def lyrics_probe(values, report_path):
    import json
    from pathlib import Path
    finder = LyricsFinder()
    try:
        track = Track(values[0], values[1], duration=float(values[2]))
        result, message = finder.find(track)
        report = dict(ok=result is not None, message=message, source=result.source if result else None,
                      title=result.title if result else None, artist=result.artist if result else None,
                      lines=len(result.lines) if result else 0,
                      first_time=result.lines[0].time if result else None,
                      last_time=result.lines[-1].time if result else None)
        if report_path:
            Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True), flush=True)
        return 0 if result else 1
    finally:
        finder.close()
