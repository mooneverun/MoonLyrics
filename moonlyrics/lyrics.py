"""Synchronized lyric parsing and conservative cross-store track matching."""
import base64
from bisect import bisect_right
from dataclasses import dataclass
import html
from itertools import islice
import logging
import re
import time
import unicodedata

from opencc import OpenCC
import requests

from .playback import finite
from .styles import normalized_source_order

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
        script = lambda value: (has_chinese(value), has_korean(value), bool(re.search(r"[A-Za-z]", value)))
        if (script(left) != script(right) and any(script(left)) and any(script(right))
                and normalize(right) not in ("live", "remix", "cover", "instrumental", "karaoke")):
            return left, right
    return ()


def artist_parts(value, *, affiliations=False):
    parts = []
    for part in re.split(r"\s*(?:[,&、，;；]|\b(?:feat(?:uring)?|ft)\.?\s+)\s*",
                         unicodedata.normalize("NFKC", value), flags=re.I):
        keys = {normalize(name) for name in (part, *bilingual_parts(part)) if normalize(name)}
        if affiliations:
            credit = re.fullmatch(r"(.+?)\s+of\s+\S.*", part, re.I)
            if credit:
                keys.update(normalize(name) for name in (credit[1], *bilingual_parts(credit[1])) if normalize(name))
        if keys and not any(keys & known for known in parts):
            parts.append(keys)
    return parts


def same_artists(left, right):
    return same_artist_groups(artist_parts(left), artist_parts(right))


def same_artist_groups(left_parts, right_parts):
    remaining = list(right_parts)
    if not left_parts or len(left_parts) != len(remaining) or len(left_parts) > 16:
        return False
    for part in left_parts:
        matches = [index for index, other in enumerate(remaining) if part & other]
        if len(matches) != 1:
            return False
        remaining.pop(matches[0])
    return True


def recording_rosters(aliases):
    """Verified recording locales may translate the lead and guest blocks independently."""
    if not aliases:
        return []
    count = len(artist_parts(aliases[0].artist + " & " + split_credit(aliases[0].title)[1]))
    rosters = []
    for lead in aliases:
        for guest in aliases:
            parts = artist_parts(lead.artist + " & " + split_credit(guest.title)[1])
            if 0 < len(parts) == count <= 16 and parts not in rosters:
                rosters.append(parts)
    return rosters


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


def metadata_rank(candidate, aliases, tolerance=5):
    """Prequalify the recording before spending requests on performer identities."""
    ranks = []
    if not candidate.title or not candidate.artist:
        return None
    title = split_credit(candidate.title)[0]
    known_titles = {title_name(split_credit(alias.title)[0]) for alias in aliases}
    if title_name(title) not in known_titles:
        bilingual = bilingual_parts(title)
        if not bilingual or not all(title_name(part) in known_titles for part in bilingual):
            return None
    for expected in aliases:
        difference = abs(candidate.duration - expected.duration)
        if expected.duration > 0 and (candidate.duration <= 0 or difference > tolerance):
            continue
        ranks.append((normalize(candidate.album) != normalize(expected.album), difference))
    return min(ranks) if ranks else None


def match_rank(candidate, aliases, tolerance=5):
    """Title AND the complete performer roster must agree; duration rejects versions."""
    rank = metadata_rank(candidate, aliases, tolerance)
    if rank is not None:
        parts = artist_parts(candidate.artist + " & " + split_credit(candidate.title)[1])
        if any(same_artist_groups(parts, roster) for roster in recording_rosters(aliases)):
            return rank
    return None


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
        self.artist_cache = {}
        self.profile_requests = 0
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
            if not matches and normalize(track.album) and track.duration > 0:
                # Song search may omit the vocal recording while the public album still lists it.
                catalog = self._json("https://itunes.apple.com/search", term=f"{track.artist} {track.album}",
                                     country=storefront, entity="song", limit=25)
                albums = catalog.get("results") if isinstance(catalog, dict) else None
                if not isinstance(albums, list):
                    self.failures.add("Apple album names")
                    albums = []
                identifiers = []
                for album in albums[:10]:
                    if (not isinstance(album, dict) or normalize(album.get("collectionName")) != normalize(track.album)
                            or not isinstance(album.get("artistName"), str) or not same_artists(album["artistName"], track.artist)):
                        continue
                    identifier = album.get("collectionId")
                    if type(identifier) is int and 0 < identifier < 10 ** 15 and identifier not in identifiers:
                        identifiers.append(identifier)
                for album_id in identifiers[:2]:
                    try:
                        catalog = self._json("https://itunes.apple.com/lookup", id=album_id, country=storefront, entity="song")
                        songs = catalog.get("results") if isinstance(catalog, dict) else None
                        if not isinstance(songs, list):
                            self.failures.add("Apple album names")
                            continue
                        songs = [item for item in songs if isinstance(item, dict) and item.get("collectionId") == album_id]
                        matches = [(match_rank(candidate, [track]), identifier, candidate)
                                   for identifier, candidate in self._apple_tracks(dict(results=songs))]
                        matches = [item for item in matches if item[0] is not None and item[1]]
                        if matches:
                            LOG.info("Recording recovered from Apple album: album_id=%s", album_id)
                            break
                    except (requests.RequestException, ValueError, TypeError, KeyError):
                        self.failures.add("Apple album names")
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

    @staticmethod
    def _artist_id(provider, record):
        identifier = record.get("id" if provider == "netease" else "mid")
        if provider == "netease":
            return identifier if type(identifier) is int and 0 < identifier < 10 ** 15 else None
        return identifier if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9]{1,64}", identifier) else None

    def _artist_profile(self, provider, record):
        self._check()
        identifier = self._artist_id(provider, record)
        if identifier is None:
            return None
        key = provider, identifier
        anchor = {normalize(name) for name in (record["name"], *bilingual_parts(record["name"]))}
        if key in self.artist_cache:
            names = self.artist_cache[key]
            return names if names & anchor else None
        if self.profile_requests >= 6:
            self.failures.add("Artist profile budget")
            return None
        self.profile_requests += 1
        try:
            if provider == "netease":
                data = self._json(f"https://music.163.com/api/artist/{identifier}", headers={"Referer": "https://music.163.com/"})
                artist = data.get("artist") if isinstance(data, dict) and data.get("code") == 200 else None
                if not isinstance(artist, dict) or type(artist.get("id")) is not int or artist["id"] != identifier:
                    raise ValueError("Artist identity mismatch")
                values = [artist.get("name"), artist.get("trans")]
                for field in ("alias", "transNames"):
                    extra = artist.get(field, [])
                    if not isinstance(extra, list) or len(extra) > 32:
                        raise ValueError("Invalid artist aliases")
                    values.extend(extra)
            else:
                body = dict(comm=dict(ct="19", cv="1859", uin="0"), req=dict(method="GetSingerDetail",
                    module="music.musichallSinger.SingerInfoInter", param=dict(singer_mids=[identifier],
                    ex_singer=1, wiki_singer=1, group_singer=0, pic=0, photos=0)))
                data = self._json("https://u.y.qq.com/cgi-bin/musicu.fcg", headers={"Referer": "https://y.qq.com/"}, body=body)
                request = data.get("req") if isinstance(data, dict) and data.get("code") == 0 else None
                details = request.get("data") if isinstance(request, dict) and request.get("code") == 0 else None
                singers = details.get("singer_list") if isinstance(details, dict) else None
                if not isinstance(singers, list) or len(singers) != 1 or not isinstance(singers[0], dict):
                    raise ValueError("Invalid artist profile")
                artist, extra = singers[0].get("basic_info"), singers[0].get("ex_info", {})
                if not isinstance(artist, dict) or artist.get("singer_mid") != identifier or not isinstance(extra, dict):
                    raise ValueError("Artist identity mismatch")
                if record.get("id") is not None and (type(record["id"]) is not int
                        or type(artist.get("singer_id")) is not int or artist["singer_id"] != record["id"]):
                    raise ValueError("Artist numeric identity mismatch")
                values = [artist.get("name"), extra.get("foreign_name")]
            if not isinstance(artist.get("name"), str) or not artist["name"].strip():
                raise ValueError("Missing artist name")
            names = set()
            for value in values:
                if value is None or value == "":
                    continue
                if not isinstance(value, str) or len(value) > 128:
                    raise ValueError("Invalid artist name")
                names.update(normalize(name) for name in (value, *bilingual_parts(value)) if normalize(name))
            if not names & anchor:
                raise ValueError("Artist name does not anchor to the search result")
            if len(self.artist_cache) >= 256:
                self.artist_cache.pop(next(iter(self.artist_cache)))
            self.artist_cache[key] = frozenset(names)
            LOG.info("Artist aliases verified: provider=%s artist_id=%s names=%s", provider, identifier, len(names))
            return self.artist_cache[key]
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            self.failures.add(provider + " artist profile")
            LOG.info("Artist profile unavailable: provider=%s error=%s", provider, type(error).__name__)
            return None

    def _source_matches(self, provider, matches, aliases):
        rosters = recording_rosters(aliases)
        known = set().union(*(part for roster in rosters for part in roster))
        known_guests = set().union(*(part for alias in aliases for part in artist_parts(split_credit(alias.title)[1]))) or known
        # Verify and download lazily: a slow later profile must not discard an earlier valid song.
        ordered = sorted(matches, key=lambda item: (match_rank(item[2], aliases) is None, item[0]))
        for rank, identifier, candidate, records in ordered[:6]:
            self._check()
            groups, identities = [], []
            for record in records:
                identity = self._artist_id(provider, record)
                names = {normalize(name) for name in (record["name"], *bilingual_parts(record["name"])) if normalize(name)}
                if identity is not None and identity in identities:
                    groups[identities.index(identity)].update(names)
                else:
                    identities.append(identity)
                    groups.append(names)
            if any(left & right for index, left in enumerate(groups) for right in groups[index + 1:]):
                continue
            if len(identities) == len(records) and match_rank(candidate, aliases) is not None:
                yield rank, identifier, candidate
                continue
            # Only featured credits may carry "of <group>"; primary artist names stay intact.
            guests = artist_parts(split_credit(candidate.title)[1], affiliations=True)
            if (not rosters or len(groups) > len(rosters[0])
                    or len(groups) + len(guests) < len(rosters[0]) or len(groups) > 16):
                continue
            unresolved_guests = any(not any(part & names for names in groups) for part in guests)
            for index, identity in enumerate(identities):
                needs_credit_alias = unresolved_guests and bool(groups[index] & known_guests)
                if identity is None or (groups[index] & known and not needs_credit_alias):
                    continue
                record = next(record for record in records if self._artist_id(provider, record) == identity)
                names = self._artist_profile(provider, record)
                if names:
                    groups[index].update(names)
            groups.extend(part for part in guests if not any(part & names for names in groups))
            if any(same_artist_groups(groups, roster) for roster in rosters):
                yield rank, identifier, candidate

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
                    rank = metadata_rank(candidate, aliases, tolerance)
                    identifier = item.get("id")
                    if rank is not None and type(identifier) is int and 0 < identifier < 10 ** 15:
                        matches.append((rank, identifier, candidate, item["artists"]))
                LOG.info("Lyric search: provider=NetEase candidates=%s recordings=%s", len(songs), len(matches))
                for _, identifier, candidate in islice(self._source_matches("netease", matches, aliases), 2):
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
                    rank = metadata_rank(candidate, aliases, tolerance)
                    mid = item.get("mid")
                    if rank is not None and isinstance(mid, str) and re.fullmatch(r"[A-Za-z0-9]{1,64}", mid):
                        matches.append((rank, mid, candidate, item["singer"]))
                LOG.info("Lyric search: provider=QQMusic candidates=%s recordings=%s", len(songs), len(matches))
                for _, mid, candidate in islice(self._source_matches("qq", matches, aliases), 2):
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

    def find(self, track, storefront="tr", cancelled=lambda: False, force=False, source_order=None):
        self.cancelled, self.deadline, self.reachable = cancelled, time.monotonic() + 20, False
        self.failures = set()
        self.profile_requests = 0
        self._check()
        order = normalized_source_order(source_order)
        key = track, storefront, order
        alias_key = track, storefront
        if not force and key in self.cache:
            cached = self.cache[key]
            return cached, f"已匹配 · {cached.source} · {len(cached.lines)} 行（缓存）"
        if force:
            self.alias_cache.pop(alias_key, None)
        self.cache.pop(key, None)
        try:
            self._check()
            base_aliases = aliases = [track]
            providers = tuple(getattr(self, "_" + source) for source in order)
            result = self._run_stage(providers[0], aliases, budget=3.)
            if result is None:
                aliases = self._run_stage(self._aliases, track, storefront, budget=3.) or aliases
                result = self._search_sources(aliases, providers if aliases != base_aliases else providers[1:], reserve=2.)
            if result is None:
                expanded = self._run_stage(self._catalog_aliases, aliases, budget=2.) or aliases
                if expanded != aliases:
                    result = self._search_sources(expanded, providers, tolerance=2)
                    if result is not None:
                        self.alias_cache[alias_key] = expanded
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
