"""Synchronized lyric parsing and conservative cross-store track matching."""
import base64
from bisect import bisect_right
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import html
import json
from itertools import islice
import logging
import math
from pathlib import Path
import re
import sqlite3
import time
import unicodedata

from opencc import OpenCC
import requests

from .playback import finite
from .styles import normalized_source_order
from . import __version__

LOG = logging.getLogger("moonlyrics.lyrics")
CACHE_LIMIT, CACHE_BYTES, CACHE_AGE = 500, 20 * 1024 * 1024, 30 * 86400
_simplified, _traditional = OpenCC("t2s"), OpenCC("s2t")
_stamp = re.compile(r"\[(\d{1,3}):([0-5]?\d)(?:[.:](\d{1,3}))?\]")
_word_stamp = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d+)?>")
_feature = r"(?:feat(?:uring)?|ft)(?:\.\s*|\s+)"
_remaster = re.compile(r"\s*[\[(（][^\])）]*(?:remaster|重制|重製)[^\])）]*[\])）]|\s*[-–—]\s*(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?", re.I)
_embedded_reading = re.compile(r"(?<=[\u3400-\u9fff])\s*(?:\(([A-Za-z]{1,16})\)|\[([A-Za-z]{1,16})\])\s*(?=[\u3400-\u9fff0-9])")
_recording_labels = {"live", "remix", "mix", "rmx", "dj", "cover", "instrumental", "karaoke", "demo",
                     "edit", "acoustic", "clean", "explicit", "mono", "stereo", "version", "ver", "extended",
                     "short", "spedup", "slowed", "sloweddown", "radio", "piano", "acapella", "reprise",
                     "rework", "remake", "unplugged", "studio", "original", "alternate", "bonus", "vip", "inst", "instr"}
_album_versions = {"live", "remix", "instrumental", "karaoke", "cover", "acoustic", "dj"}


def normalize(text):
    value = _simplified.convert(unicodedata.normalize("NFKC", str(text or ""))).casefold()
    value = unicodedata.normalize("NFKD", value).replace("ı", "i")
    return "".join(c for c in value if c.isalnum() and not unicodedata.combining(c))


def title_name(text):
    value = _remaster.sub("", unicodedata.normalize("NFKC", str(text or "")))
    # Only attached, internal readings are optional; standalone bilingual titles still need verified aliases.
    def keep_version(match):
        return match[0] if (match[1] or match[2]).casefold() in _recording_labels else ""
    return normalize(_embedded_reading.sub(keep_version, value))


def has_chinese(text):
    return any("\u3400" <= char <= "\u9fff" for char in text)


def has_korean(text):
    return any("\uac00" <= char <= "\ud7af" or "\u1100" <= char <= "\u11ff" for char in text)


def split_credit(text):
    text = _remaster.sub("", unicodedata.normalize("NFKC", str(text or ""))).strip()
    match = re.search(r"\s*([\[(])\s*(?:" + _feature + r"|with\s+)", text, re.I)
    if match:
        brackets = {"(": ")", "[": "]"}
        stack = [brackets[match[1]]]
        for index in range(match.end(), len(text)):
            char = text[index]
            if char in brackets:
                stack.append(brackets[char])
            elif char in ")]":
                if char != stack.pop():
                    break
                if not stack:
                    guests = text[match.end():index].strip()
                    if guests:
                        return (text[:match.start()] + " " + text[index + 1:]).strip(), guests
                    break
        return text, ""
    match = re.search(r"\s+" + _feature + r"(.+)$", text, re.I)
    if match:
        return text[:match.start()].strip(), match[1].strip()
    return text, ""


def bilingual_parts(text):
    value = unicodedata.normalize("NFKC", text).strip()
    match = (re.fullmatch(r"(.+?)\s*[\[(]([^\[\]()]+)[\])]", value)
             or re.fullmatch(r"([^:]+):([^:]+)", value))
    if match:
        left, right = match[1].strip(), match[2].strip()
        script = lambda value: (has_chinese(value), has_korean(value), bool(re.search(r"[A-Za-z]", value)))
        if (script(left) != script(right) and any(script(left)) and any(script(right))
                and normalize(right) not in ("live", "remix", "cover", "instrumental", "karaoke")):
            return left, right
    return ()


def artist_parts(value, *, affiliations=False):
    parts = []
    for part in re.split(r"\s*(?:[,&、，;；]|\b" + _feature + r")\s*",
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
    count = max(len(artist_parts(alias.artist + " & " + split_credit(alias.title)[1])) for alias in aliases)
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
    title_aliases: tuple = ()


def catalog_names(record):
    """Only structured, bounded names from the same catalog object are aliases."""
    names = []
    for key in ("name", "title", "trans", "foreign_name", "alias", "alia", "transNames", "tns"):
        values = record.get(key) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (tuple, list)) or len(values) > 32:
            continue
        names.extend(value.strip() for value in values
                     if isinstance(value, str) and 0 < len(value.strip()) <= 256)
    return tuple(dict.fromkeys(names))


def name_forms(value, *, album=False):
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    if album:
        value = re.sub(r"\s*[-–—]\s*(?:single|ep)$", "", value, flags=re.I).strip()
    parts = list(bilingual_parts(value))
    for space in re.finditer(r"\s+", value):
        left, right = value[:space.start()], value[space.end():]
        if normalize(left) in _recording_labels or normalize(right) in _recording_labels:
            continue
        native = lambda text: has_chinese(text) or has_korean(text)
        latin = lambda text: bool(re.search(r"[A-Za-z]", text))
        if (native(left) and not latin(left) and latin(right) and not native(right)
                or native(right) and not latin(right) and latin(left) and not native(left)):
            parts.extend((left, right))
    return {title_name(part) for part in (value, *parts) if title_name(part)}


@dataclass(frozen=True)
class MatchEvidence:
    title: bool
    fragment: bool
    album: bool
    gap: float
    conflict: bool


def recording_evidence(candidate, aliases):
    gap = min((abs(candidate.duration - alias.duration) for alias in aliases if alias.duration > 0), default=float("inf"))
    title = split_credit(candidate.title)[0]
    fragment = any(name_forms(title) & name_forms(split_credit(alias.title)[0]) for alias in aliases)
    album = bool(candidate.album) and any(alias.album and name_forms(candidate.album, album=True)
                                         & name_forms(alias.album, album=True) for alias in aliases)
    conflict = (candidate.duration <= 0 or gap > 5 or not any(
        recording_version(candidate.title) == recording_version(alias.title)
        and recording_version(candidate.album) & _album_versions == recording_version(alias.album) & _album_versions
        for alias in aliases))
    return MatchEvidence(metadata_rank(candidate, aliases) is not None, fragment, album, gap, conflict)


@dataclass
class LyricCandidate:
    provider: str
    identifier: object
    track: Track
    records: tuple
    payload: dict
    proof: str = ""


def recording_version(title):
    title = split_credit(title)[0]
    blocks = re.findall(r"[\[(（]([^\])）]+)[\])）]|\s[-–—]\s(.+)$", title)
    labels = set()
    for block in blocks:
        text = " ".join(block).casefold()
        labels.update(word for word in re.findall(r"[a-z]+", text) if word in _recording_labels)
        labels.update(word for word in ("现场", "現場", "伴奏", "翻唱", "纯音乐", "純音樂") if word in text)
    equivalents = {"inst": "instrumental", "instr": "instrumental", "rmx": "remix", "现场": "live", "現場": "live",
                   "伴奏": "instrumental", "翻唱": "cover", "纯音乐": "instrumental", "純音樂": "instrumental"}
    return {equivalents.get(label, label) for label in labels}


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


def parse_synced_lrc(text, duration):
    """A single timestamped placeholder must not hide another source's real lyrics."""
    try:
        lines = parse_lrc(text)
    except (ValueError, OverflowError):
        return ()
    usable = [line for line in lines if line.text and (duration <= 0 or line.time <= duration + 5)]
    return lines if len(usable) >= 2 else ()


def metadata_rank(candidate, aliases, tolerance=5):
    """Prequalify the recording before spending requests on performer identities."""
    ranks = []
    if not candidate.title or not candidate.artist:
        return None
    title = split_credit(candidate.title)[0]
    known_titles = {title_name(split_credit(alias.title)[0]) for alias in aliases}
    if title_name(title) not in known_titles:
        bilingual = bilingual_parts(title)
        translated = any(title_name(split_credit(name)[0]) in known_titles
                         and any(recording_version(candidate.title) == recording_version(alias.title) for alias in aliases)
                         for name in candidate.title_aliases)
        if not translated and (not bilingual or not all(title_name(part) in known_titles for part in bilingual)):
            return None
    for expected in aliases:
        if recording_version(candidate.album) & _album_versions != recording_version(expected.album) & _album_versions:
            continue
        difference = abs(candidate.duration - expected.duration)
        if expected.duration > 0 and (candidate.duration <= 0 or difference > tolerance):
            continue
        ranks.append((normalize(candidate.album) != normalize(expected.album), difference))
    return min(ranks) if ranks else None


def match_rank(candidate, aliases, tolerance=5):
    """Title AND the complete performer roster must agree; duration rejects versions."""
    # Wider search recall must not adopt same-named language versions a few seconds apart.
    rank = metadata_rank(candidate, aliases, min(tolerance, 1.5))
    if rank is not None:
        parts = artist_parts(candidate.artist + " & " + split_credit(candidate.title)[1])
        rosters = [parts]
        guests = artist_parts(split_credit(candidate.title)[1])
        primary = artist_parts(candidate.artist)
        for guest in guests:
            matches = [index for index, part in enumerate(primary) if part & guest]
            if len(matches) == 1:
                primary.pop(matches[0])
        # A catalog's translated title can translate the complete featured block as well as the song name.
        for title in candidate.title_aliases:
            translated = artist_parts(split_credit(title)[1], affiliations=True)
            if (guests and len(translated) == len(guests)
                    and recording_version(title) == recording_version(candidate.title)):
                rosters.append(primary + translated)
        if any(same_artist_groups(parts, roster) for parts in rosters for roster in recording_rosters(aliases)):
            return rank
    return None


class SearchCancelled(Exception):
    pass


class StageExpired(Exception):
    pass


class LyricsFinder:
    def __init__(self, cache_dir=None):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "MoonLyrics/0.1 (https://github.com/mooneverun/MoonLyrics)"
        self.cache = {}
        self.cache_path = Path(cache_dir) / "lyrics.sqlite3" if cache_dir is not None else None
        self.alias_cache = {}
        self.artist_cache = {}
        self.artist_round_cache = {}
        self.profile_requests = 0
        self.stage_deadline = None
        self.failures = set()
        self.apple_records = {}
        self.album_cache = {}
        self.candidate_albums = {}
        self.round_cache = {}
        self.plain_found = self.uncertain_found = False
        self.lrclib_next = 0.
        self.lrclib_retry_at = 0.
        self.candidates = {}
        self.cover_cache = {}
        self.artist_albums = {}
        self.current_track = None
        self.current_storefront = "tr"
        self.retryable = True
        self.diagnostics = {}
        self.attempted_lyrics = set()
        self.request_count = 0
        self.selected_candidate = None
        self.lookup_key = None
        self.lookup_started = 0.
        self.endpoint_retry_at = {}

    def close(self):
        self.session.close()

    def _cache_key(self, track, storefront, order):
        # Keep punctuation and version labels; upgrades must recheck old matching decisions.
        names = [" ".join(unicodedata.normalize("NFKC", name).casefold().split())
                 for name in (track.title, track.artist, track.album)]
        return sha256(json.dumps([1, __version__, names, storefront, order], ensure_ascii=False).encode()).hexdigest()

    def _cached_lyrics(self, track, storefront, order):
        if self.cache_path is None or track.duration <= 0:
            return None
        try:
            if not self.cache_path.is_file():
                return None
            with closing(sqlite3.connect(self.cache_path, timeout=.1)) as db, db:
                db.execute("PRAGMA cache_size=-256")
                row = db.execute("SELECT duration, data FROM lyrics WHERE identity=? AND ABS(duration-?)<=0.25 "
                                 "AND saved>=? AND length(CAST(data AS BLOB))<=1000000 "
                                 "ORDER BY ABS(duration-?) LIMIT 1",
                                 (self._cache_key(track, storefront, order), track.duration,
                                  time.time() - CACHE_AGE, track.duration)).fetchone()
                if row is None:
                    return None
                data = json.loads(row[1])
                if (not isinstance(data, dict) or data.get("source") not in ("酷狗", "LRCLIB", "网易云音乐", "QQ音乐")
                        or any(not isinstance(data.get(key), str) or not 0 < len(data[key]) <= 1024
                               for key in ("title", "artist"))
                        or not isinstance(data.get("lines"), list) or not 2 <= len(data["lines"]) <= 8000):
                    return None
                lines = []
                for line in data["lines"]:
                    if (not isinstance(line, list) or len(line) != 2 or type(line[0]) not in (int, float)
                            or not math.isfinite(line[0]) or not 0 <= line[0] <= 8 * 3600
                            or not isinstance(line[1], str) or len(line[1]) > 16000
                            or lines and line[0] <= lines[-1].time):
                        return None
                    lines.append(LyricLine(line[0], line[1]))
                if sum(bool(line.text.strip()) and line.time <= track.duration + 5 for line in lines) < 2:
                    return None
                db.execute("UPDATE lyrics SET used=? WHERE identity=? AND duration=?",
                           (time.time(), self._cache_key(track, storefront, order), row[0]))
                return Lyrics(tuple(lines), data["source"], data["title"], data["artist"])
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            LOG.warning("Local lyric cache unavailable; continuing with online matching")
        return None

    def _cache_lyrics(self, track, storefront, order, document, selected):
        if self.cache_path is None or selected is None or not math.isfinite(track.duration) or track.duration <= 0:
            return
        try:
            data = json.dumps(dict(source=document.source, title=document.title, artist=document.artist,
                                   lines=[(line.time, line.text) for line in document.lines], selected=selected),
                              ensure_ascii=False, allow_nan=False)
            if len(data.encode("utf-8")) > 1_000_000:
                return
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.cache_path, timeout=.1)) as db, db:
                db.execute("PRAGMA cache_size=-256")
                db.execute("CREATE TABLE IF NOT EXISTS lyrics (identity TEXT NOT NULL, duration REAL NOT NULL, "
                           "data TEXT NOT NULL, saved REAL NOT NULL, used REAL NOT NULL, PRIMARY KEY(identity,duration))")
                now = time.time()
                db.execute("INSERT OR REPLACE INTO lyrics VALUES(?,?,?,?,?)",
                           (self._cache_key(track, storefront, order), track.duration, data, now, now))
                db.execute("DELETE FROM lyrics WHERE saved<?", (now - CACHE_AGE,))
                rows = db.execute("SELECT identity,duration,length(CAST(data AS BLOB)) FROM lyrics ORDER BY used DESC").fetchall()
                size = 0
                for index, (identity, duration, length) in enumerate(rows):
                    size += length
                    if index >= CACHE_LIMIT or size > CACHE_BYTES:
                        db.execute("DELETE FROM lyrics WHERE identity=? AND duration=?", (identity, duration))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            LOG.warning("Could not save local lyrics; playback is unaffected")

    def clear_cache(self):
        self.cache.clear()
        self.round_cache.clear()
        self.candidates.clear()
        if self.cache_path is not None:
            try:
                self.cache_path.unlink(missing_ok=True)
            except OSError:
                LOG.warning("Could not clear local lyric cache")
                return False
        return True

    def _remember(self, provider, identifier, track, records=(), payload=None):
        if not identifier or not track.title or not track.artist or track.duration <= 0:
            return
        self.candidates[provider, identifier] = LyricCandidate(provider, identifier, track, tuple(records), payload or {})
        if len(self.candidates) > 64:
            aliases = self.alias_cache.get((self.current_track, self.current_storefront), [self.current_track])
            def relevance(key):
                item = self.candidates[key]
                if not self.current_track:
                    return (True, True, item.track.duration)
                facts = recording_evidence(item.track, aliases)
                return (not facts.title, not facts.fragment, facts.gap)
            self.candidates.pop(max(reversed(self.candidates), key=relevance))

    def _download(self, candidate):
        key = candidate.provider, candidate.identifier
        if key in self.attempted_lyrics:
            return None
        provider, identifier = key
        try:
            if provider == "netease":
                data = self._json("https://music.163.com/api/song/lyric", headers={"Referer": "https://music.163.com/"},
                                  id=identifier, lv=-1, kv=-1, tv=-1)
                if not isinstance(data, dict) or data.get("code") != 200 or not isinstance(data.get("lrc"), dict):
                    raise ValueError("Invalid NetEase lyric response")
                raw = "" if data.get("nolyric") else data["lrc"].get("lyric") or ""
            elif provider == "qq":
                data = self._json("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                                  headers={"Referer": "https://y.qq.com/"}, songmid=identifier, format="json", nobase64=1)
                if not isinstance(data, dict) or data.get("code") != 0:
                    raise ValueError("Invalid QQ lyric response")
                raw = data.get("lyric") or ""
            elif provider == "kugou":
                data = self._json("https://lyrics.kugou.com/download", ver=1, client="pc", id=identifier,
                                  accesskey=candidate.payload["accesskey"], fmt="lrc", charset="utf8")
                if not isinstance(data, dict):
                    raise ValueError("Invalid Kugou lyric response")
                raw = base64.b64decode(data.get("content", ""), validate=True).decode("utf-8-sig")
            else:
                raw = candidate.payload.get("syncedLyrics") or candidate.payload.get("plainLyrics") or ""
            raw = html.unescape(raw)
            lines = parse_synced_lrc(raw, candidate.track.duration)
            if lines:
                self.selected_candidate = candidate
                return Lyrics(lines, {"netease": "网易云音乐", "qq": "QQ音乐", "kugou": "酷狗", "lrclib": "LRCLIB"}[provider],
                              candidate.track.title, candidate.track.artist)
            self.attempted_lyrics.add(key)
            self.plain_found |= len(raw.strip()) >= 20 and not _stamp.search(raw)
        except (requests.RequestException, ValueError, TypeError, KeyError, UnicodeError):
            self.failures.add(provider + " lyrics")
        return None

    def _resolve_candidates(self, aliases, order, *, deep=False):
        for provider in order:
            retained = [item for item in self.candidates.values() if item.provider == provider]
            matches = []
            for item in retained:
                facts = recording_evidence(item.track, aliases)
                if facts.conflict:
                    continue
                rank = (not facts.title, not facts.album, facts.gap)
                matches.append((rank, item.identifier, item.track, item.records))
            accepted = (self._source_matches(provider, matches, aliases) if provider in ("netease", "qq")
                        else (row[:3] for row in sorted(matches, key=lambda row: row[0])
                              if match_rank(row[2], aliases) is not None))
            for _, identifier, _ in islice(accepted, 2):
                result = self._download(self.candidates[provider, identifier])
                if result:
                    return result
            if deep and provider in ("netease", "qq"):
                verified = [item for item in retained if self._verify_single(item, aliases)]
                # Several different credit sets remain ambiguous even when their releases look alike.
                credits = {tuple(sorted(normalize(record.get("name")) for record in item.records)) for item in verified}
                if len(credits) > 1:
                    self.uncertain_found = True
                    continue
                for item in verified[:2]:
                    result = self._download(item)
                    if result:
                        return result
        return None

    def _artist_catalog(self, track, storefront):
        if not track.album:
            return
        payload = self._json("https://itunes.apple.com/search", term=track.artist,
                             country=storefront, entity="musicArtist", limit=10)
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        artists = [row for row in rows[:10] if isinstance(row, dict) and type(row.get("artistId")) is int
                   and row["artistId"] > 0 and same_artists(str(row.get("artistName") or ""), track.artist)]
        if not artists:
            return
        artists.sort(key=lambda row: row["artistName"].casefold() != track.artist.casefold())
        matches = []
        for artist in artists[:3]:
            key = artist["artistId"], storefront
            albums = self.artist_albums.get(key)
            if albums is None:
                data = self._json("https://itunes.apple.com/lookup", id=artist["artistId"], country=storefront,
                                  entity="album", limit=200)
                albums = data.get("results", []) if isinstance(data, dict) else []
                if not isinstance(albums, list):
                    continue
                if len(self.artist_albums) >= 16:
                    self.artist_albums.pop(next(iter(self.artist_albums)))
                self.artist_albums[key] = albums
            matches.extend(row for row in albums[:200] if isinstance(row, dict) and type(row.get("collectionId")) is int
                           and row["collectionId"] > 0 and name_forms(row.get("collectionName"), album=True)
                           & name_forms(track.album, album=True)
                           and same_artists(str(row.get("artistName") or ""), track.artist))
        verified = []
        for album in matches[:2]:
            data = self._json("https://itunes.apple.com/lookup", id=album["collectionId"], country=storefront,
                              entity="song", limit=200)
            for row in data.get("results", []) if isinstance(data, dict) else []:
                if not isinstance(row, dict) or row.get("collectionId") != album["collectionId"]:
                    continue
                for identifier, candidate in self._apple_tracks(dict(results=[row])):
                    if identifier and match_rank(candidate, [track], tolerance=1.5) is not None:
                        verified.append(row)
        if len({row["trackId"] for row in verified}) == 1:
            if len(self.apple_records) >= 64:
                self.apple_records.pop(next(iter(self.apple_records)))
            self.apple_records[track, storefront] = verified[0]

    def _same_cover(self, left, right):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QImage
        signatures = []
        for url in (left, right):
            if not isinstance(url, str):
                return False
            url = url.replace("http://", "https://", 1)
            if not re.fullmatch(r"https://(?:is\d-ssl\.mzstatic\.com|p\d\.music\.126\.net|y\.gtimg\.cn)/[^\s]{1,1500}", url):
                return False
            if re.match(r"https://p\d\.music\.126\.net/", url) and "?" not in url:
                url += "?param=100y100"
            if url not in self.cover_cache:
                self._check()
                remaining = min(self.deadline, self.stage_deadline or self.deadline) - time.monotonic()
                self.request_count += 1
                response = self.session.get(url, timeout=max(.1, min(2., remaining)))
                response.raise_for_status()
                if len(response.content) > 1_000_000:
                    return False
                image = QImage.fromData(response.content)
                if image.isNull() or max(image.width(), image.height()) > 4096:
                    return False
                image = image.scaled(16, 16, transformMode=Qt.TransformationMode.SmoothTransformation)
                signature = tuple(channel for y in range(16) for x in range(16)
                                  for channel in image.pixelColor(x, y).getRgb()[:3])
                if len(self.cover_cache) >= 32:
                    self.cover_cache.pop(next(iter(self.cover_cache)))
                self.cover_cache[url] = signature
                self._check()
            signatures.append(self.cover_cache[url])
        left, right = signatures
        # Flat placeholder art cannot establish a release identity.
        if any(sum(sum((value - sum(values[channel::3]) / 256) ** 2 for value in values[channel::3])
                       for channel in range(3)) / len(values) < 100 for values in signatures):
            return False
        return sum(abs(a - b) for a, b in zip(left, right)) / len(left) <= 8

    def _verify_single(self, item, aliases):
        facts = recording_evidence(item.track, aliases)
        original = self.apple_records.get((self.current_track, self.current_storefront))
        if (facts.conflict or not facts.fragment or not facts.album or facts.gap > .5 or not original
                or original.get("trackCount") != 1 or item.provider not in ("netease", "qq")):
            return False
        source = artist_parts(item.track.artist + " & " + split_credit(item.track.title)[1])
        rosters = recording_rosters(aliases)
        partial = False
        for expected in rosters:
            small, large = sorted((source, expected), key=len)
            partial |= bool(small) and len(small) != len(large) and all(
                sum(bool(part & other) for other in large) == 1 for part in small)
        if not partial:
            return False
        identities = [self._artist_id(item.provider, record) for record in item.records]
        if None in identities or len(set(identities)) != len(identities):
            return False
        try:
            if item.provider == "netease":
                album_id = item.payload.get("album", item.payload.get("al", {})).get("id")
                if type(album_id) is not int or not 0 < album_id < 10 ** 15:
                    return False
                data = self._json(f"https://music.163.com/api/album/{album_id}", id=album_id,
                                  headers={"Referer": "https://music.163.com/"})
                album = data.get("album") if isinstance(data, dict) and data.get("code") == 200 else None
                songs = data.get("songs", album.get("songs")) if isinstance(album, dict) else None
                if (not isinstance(album, dict) or album.get("id") != album_id or not isinstance(songs, list)
                        or len(songs) != 1 or not isinstance(songs[0], dict) or songs[0].get("id") != item.identifier):
                    return False
                candidate, _ = self._netease_record(songs[0])
                source_day = datetime.fromtimestamp(finite(album.get("publishTime")) / 1000, timezone.utc).date()
                cover = album.get("picUrl")
            else:
                album = item.payload.get("album", {})
                mid = album.get("mid")
                if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", mid):
                    return False
                body = dict(comm=dict(ct="19", cv="1859", uin="0"),
                            req=dict(module="music.musichallAlbum.AlbumInfoServer", method="GetAlbumDetail",
                                     param=dict(albumMid=mid)))
                data = self._json("https://u.y.qq.com/cgi-bin/musicu.fcg", headers={"Referer": "https://y.qq.com/"}, body=body)
                request = data.get("req") if isinstance(data, dict) and data.get("code") == 0 else None
                details = request.get("data") if isinstance(request, dict) and request.get("code") == 0 else None
                info = details.get("basicInfo") if isinstance(details, dict) else None
                if not isinstance(info, dict):
                    raise ValueError("Invalid QQ album detail response")
                if info.get("albumMid") != mid or info.get("albumType") != "Single":
                    return False
                body = dict(body, req=dict(module="music.musichallAlbum.AlbumSongList", method="GetAlbumSongList",
                            param=dict(albumMid=mid, albumID=album.get("id"), begin=0, num=10, order=2)))
                data = self._json("https://u.y.qq.com/cgi-bin/musicu.fcg", headers={"Referer": "https://y.qq.com/"}, body=body)
                request = data.get("req") if isinstance(data, dict) and data.get("code") == 0 else None
                listing = request.get("data") if isinstance(request, dict) and request.get("code") == 0 else None
                if not isinstance(listing, dict):
                    raise ValueError("Invalid QQ album song response")
                songs = listing.get("songList", [])
                if (listing.get("albumMid") != mid or listing.get("totalNum") != 1 or not isinstance(songs, list)
                        or len(songs) != 1 or not isinstance(songs[0], dict)):
                    return False
                song = songs[0].get("songInfo", {})
                if not isinstance(song, dict) or not isinstance(song.get("album"), dict):
                    return False
                if song.get("mid") != item.identifier or song["album"].get("mid") != mid:
                    return False
                records = song.get("singer", [])
                if not isinstance(records, list) or not all(isinstance(record, dict) and isinstance(record.get("name"), str)
                                                          for record in records):
                    return False
                candidate = Track(str(song.get("title") or song.get("name") or ""),
                                  " & ".join(record["name"] for record in records),
                                  str(song.get("album", {}).get("name") or ""), finite(song.get("interval")))
                source_day = datetime.fromisoformat(info["publishDate"]).date()
                cover = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{mid}.jpg"
            original_duration = finite(original.get("trackTimeMillis")) / 1000
            if (candidate is None or title_name(candidate.title) != title_name(item.track.title)
                    or not same_artists(candidate.artist, item.track.artist)
                    or abs(candidate.duration - original_duration) > (.15 if item.provider == "netease" else 1.)
                    or not original.get("releaseDate")):
                return False
            apple_day = datetime.fromisoformat(original["releaseDate"].replace("Z", "+00:00")).date()
            verified = source_day == apple_day and self._same_cover(original.get("artworkUrl100"), cover)
            if verified:
                item.proof = "single_release_artwork_date_timing_credits"
            return verified
        except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError, OSError):
            self.failures.add("Single release verification")
            return False

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
            if result is not None:
                self.stage_deadline = previous
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
        key = url, json.dumps((params, body, headers), sort_keys=True, ensure_ascii=True)
        if key in self.round_cache:
            return self.round_cache[key]
        if time.monotonic() < self.endpoint_retry_at.get(url, 0):
            self.failures.add(url)
            raise requests.RequestException("Endpoint is temporarily limited; respecting backoff")
        if "lrclib.net/" in url:
            delay = self.lrclib_next - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, .5))
                self._check()
            self.lrclib_next = time.monotonic() + .25
        response = None
        try:
            remaining = min(self.deadline, self.stage_deadline or self.deadline) - time.monotonic()
            if remaining <= .1:
                if self.deadline - time.monotonic() <= .1:
                    raise TimeoutError("歌词匹配超时，可以重新尝试")
                raise StageExpired()
            options = dict(timeout=(min(1.5, remaining * .35), min(3., remaining * .65)))
            self.request_count += 1
            if headers:
                options["headers"] = headers
            response = (self.session.get(url, params=params, **options) if body is None
                        else self.session.post(url, params=params, json=body, **options))
            response.raise_for_status()
            if len(response.content) > 3_000_000:
                raise ValueError("Lyrics response is too large")
            data = response.json()
            if isinstance(data, dict) and data.get("code") in (-462, 301):
                self.endpoint_retry_at[url] = time.monotonic() + 60
                raise requests.RequestException("Anonymous catalog access is temporarily limited")
        except (requests.RequestException, ValueError) as exc:
            self.failures.add(url)
            if getattr(response, "status_code", None) in (429, 503):
                try:
                    seconds = max(2., min(120., float(response.headers.get("Retry-After", "15"))))
                except (ValueError, TypeError):
                    seconds = 15.
                self.endpoint_retry_at[url] = time.monotonic() + seconds
            LOG.info("Lyric request failed: endpoint=%s status=%s error=%s",
                     url, getattr(response, "status_code", None), type(exc).__name__)
            raise
        if (len(self.round_cache) < 96 and (not isinstance(data, dict)
                or data.get("code", 0) in (0, 200) and data.get("status", 200) in (1, 200)
                and (not isinstance(data.get("req"), dict) or data["req"].get("code", 0) == 0))):
            self.round_cache[key] = data
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
            try:
                payload = self._json("https://itunes.apple.com/search", term=f"{track.title} {track.artist}",
                                     country=storefront, entity="song", limit=25)
            except (requests.RequestException, ValueError, TypeError, KeyError):
                self.failures.add("Apple song search")
                payload = dict(results=[])
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                self.failures.add("Apple")
                payload = dict(results=[])
            matches = [(match_rank(candidate, [track]), identifier, candidate)
                       for identifier, candidate in self._apple_tracks(payload)]
            catalog_records = payload["results"]
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
                            catalog_records = songs
                            LOG.info("Recording recovered from Apple album: album_id=%s", album_id)
                            break
                    except (requests.RequestException, ValueError, TypeError, KeyError):
                        self.failures.add("Apple album names")
            if matches:
                _, identifier, original = min(matches, key=lambda item: item[0])
                record = next((item for item in catalog_records if isinstance(item, dict)
                               and item.get("trackId") == identifier), {})
                if type(record.get("collectionId")) is int and record["collectionId"] > 0:
                    if len(self.apple_records) >= 64:
                        self.apple_records.pop(next(iter(self.apple_records)))
                    self.apple_records[key] = record
                chinese = has_chinese(track.title + track.artist + track.album)
                checked_korean = False
                for country in (("hk", "tw", "kr") if chinese else ("kr", "hk", "tw")):
                    if country == "tw" and any(has_chinese(alias.title) and has_chinese(alias.artist) for alias in aliases):
                        continue
                    checked_korean |= country == "kr"
                    try:
                        localized = self._json("https://itunes.apple.com/lookup", id=identifier, country=country, entity="song")
                        if not isinstance(localized, dict) or not isinstance(localized.get("results"), list):
                            self.failures.add("Apple localized names")
                            continue
                        for found_id, candidate in self._apple_tracks(localized):
                            if (found_id != identifier or not candidate.title or not candidate.artist
                                    or abs(candidate.duration - original.duration) > 1):
                                continue
                            if not any(normalize(candidate.title) == normalize(alias.title)
                                       and normalize(candidate.artist) == normalize(alias.artist) for alias in aliases):
                                aliases.append(candidate)
                                if key not in self.alias_cache and len(self.alias_cache) >= 64:
                                    self.alias_cache.pop(next(iter(self.alias_cache)))
                                self.alias_cache[key] = aliases
                    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
                        self.failures.add("Apple localized names")
                        LOG.info("Localized name lookup failed: country=%s error=%s", country, type(error).__name__)
                    if any(has_korean(alias.title + alias.artist)
                           or checked_korean and has_chinese(alias.title) and has_chinese(alias.artist) for alias in aliases):
                        break
                paired = self.album_cache.get((record.get("collectionId"), storefront), ())
                for apple, source, _ in paired:
                    if apple["trackId"] == identifier and source not in aliases:
                        aliases.append(source)
        except (requests.RequestException, ValueError, TypeError, KeyError):
            LOG.debug("Apple localized name lookup unavailable", exc_info=True)
        self._check()
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
        queries = list(dict.fromkeys(_simplified.convert(f"{t.artist} - {title}").strip() for t in reversed(aliases)
                                    for title in (split_credit(t.title)[0], t.title)))
        for query in queries[:4]:
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
                    if abs(candidate.duration - aliases[0].duration) <= tolerance and item.get("accesskey"):
                        self._remember("kugou", item.get("id"), candidate, payload=item)
                    rank = match_rank(candidate, aliases, tolerance)
                    if rank is not None and item.get("id") and item.get("accesskey"):
                        matches.append((rank, item, candidate))
                LOG.info("Lyric search: provider=KuGou candidates=%s accepted=%s", len(candidates), len(matches))
                for _, item, candidate in sorted(matches, key=lambda item: item[0])[:2]:
                    result = self._download(LyricCandidate("kugou", item["id"], candidate, (), item))
                    if result:
                        return result
            except (requests.RequestException, ValueError, TypeError, KeyError, UnicodeError):
                LOG.debug("KuGou lyrics unavailable", exc_info=True)
        return None

    def _lrclib(self, aliases, tolerance=5):
        if time.monotonic() < self.lrclib_retry_at:
            self.failures.add("LRCLIB rate limit")
            return None
        queries = []
        for track in aliases:
            title = split_credit(track.title)[0]
            if not has_chinese(title):
                queries.append((title, track.artist))
            queries.append((title, None))
        for track in aliases:
            for title in (track.title, _simplified.convert(track.title), _traditional.convert(track.title)):
                if not has_chinese(title):
                    queries.append((title, track.artist))
                queries.append((title, None))
        for title, artist in list(dict.fromkeys(queries))[:6]:
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
                for index, item in enumerate(data[:200]):
                    if not isinstance(item, dict) or item.get("instrumental") is True:
                        continue
                    candidate = Track(str(item.get("trackName") or ""), str(item.get("artistName") or ""),
                                      str(item.get("albumName") or ""), finite(item.get("duration")))
                    identifier = item.get("id") or (candidate.title, candidate.artist, candidate.album, candidate.duration, index)
                    if abs(candidate.duration - aliases[0].duration) <= tolerance:
                        self._remember("lrclib", identifier, candidate, payload=item)
                    rank = match_rank(candidate, aliases, tolerance)
                    if rank is not None:
                        matches.append((rank, identifier, item, candidate))
                LOG.info("Lyric search: provider=LRCLIB candidates=%s accepted=%s", len(data), len(matches))
                for _, identifier, item, candidate in sorted(matches, key=lambda item: item[0]):
                    result = self._download(LyricCandidate("lrclib", identifier, candidate, (), item))
                    if result:
                        return result
            except (requests.RequestException, ValueError, TypeError, KeyError) as error:
                LOG.debug("LRCLIB lyrics unavailable", exc_info=True)
                if getattr(getattr(error, "response", None), "status_code", None) == 429:
                    try:
                        seconds = max(1., float(error.response.headers.get("Retry-After", "60")))
                    except (TypeError, ValueError):
                        seconds = 60.
                    self.lrclib_retry_at = time.monotonic() + seconds
                    break
        return None

    @staticmethod
    def _netease_record(item):
        records = item.get("artists", item.get("ar"))
        album = item.get("album", item.get("al"))
        if (not isinstance(records, list) or not 0 < len(records) <= 16 or not isinstance(album, dict)
                or not all(isinstance(artist, dict) and isinstance(artist.get("name"), str)
                           and artist["name"].strip() for artist in records)):
            return None, []
        return Track(str(item.get("name") or ""), " & ".join(artist["name"] for artist in records),
                     str(album.get("name") or ""), finite(item.get("duration", item.get("dt"))) / 1000,
                     catalog_names(item)), records

    @staticmethod
    def _align_album(apple, source):
        """A whole album's timing and multiple title anchors establish the recording, never a lone duration."""
        if not 6 <= len(apple) == len(source) <= 60:
            return None
        if len({item.get("id") for item in source if isinstance(item, dict)}) != len(source):
            return None
        pairs, anchors, precise = [], 0, 0
        for index, (original, item) in enumerate(zip(apple, source)):
            if not isinstance(item, dict) or type(item.get("id")) is not int or not 0 < item["id"] < 10 ** 15:
                return None
            candidate, records = LyricsFinder._netease_record(item)
            expected = next((track for _, track in LyricsFinder._apple_tracks(dict(results=[original]))), None)
            number = item.get("no")
            if (expected is None or candidate is None or candidate.duration <= 0 or expected.duration <= 0
                    or type(number) is not int or number not in (original.get("trackNumber"), index + 1)
                    or recording_version(expected.title) != recording_version(candidate.title)
                    or recording_version(expected.album) & _album_versions != recording_version(candidate.album) & _album_versions):
                return None
            difference = abs(expected.duration - candidate.duration)
            if difference > 1.5:
                return None
            precise += difference <= .5
            # An incomplete credit on another track must not discard the album; never learn that track's names.
            if (len(artist_parts(candidate.artist)) == len(artist_parts(expected.artist + " & " + split_credit(expected.title)[1]))
                    and all(LyricsFinder._artist_id("netease", record) is not None for record in records)
                    and len({record["id"] for record in records}) == len(records)):
                anchors += metadata_rank(candidate, [expected], tolerance=1.5) is not None
                localized = Track(split_credit(candidate.title)[0], candidate.artist, candidate.album, candidate.duration,
                                  candidate.title_aliases)
                pairs.append((original, localized, records))
        if anchors < 2 or precise < max(6, len(apple) * .8):
            return None
        return pairs

    def _album_aliases(self, track, storefront, aliases):
        original = self.apple_records.get((track, storefront))
        if not original:
            return aliases
        album_id, identifier = original["collectionId"], original["trackId"]
        key = album_id, storefront
        pairs = self.album_cache.get(key)
        if pairs is None:
            payload = self._json("https://itunes.apple.com/lookup", id=album_id, country=storefront,
                                 entity="song", limit=200)
            records = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                return aliases
            apple = [row for row in records if isinstance(row, dict) and row.get("kind") == "song"
                     and row.get("collectionId") == album_id]
            if (not 6 <= len(apple) <= 60 or len({row.get("trackId") for row in apple}) != len(apple)
                    or any(row.get("trackCount") != len(apple) for row in apple)
                    or not any(row.get("trackId") == identifier for row in apple)):
                return aliases
            positions = [(row.get("discNumber"), row.get("trackNumber")) for row in apple]
            if (any(type(disc) is not int or type(number) is not int or min(disc, number) < 1
                    for disc, number in positions) or len(set(positions)) != len(positions)):
                return aliases
            apple.sort(key=lambda row: (row["discNumber"], row["trackNumber"]))
            # Guest tracks are useful cross-language anchors; search titles only, then verify the entire album.
            anchors = sorted(apple, key=lambda row: (not bool(split_credit(row["trackName"])[1]),
                bool(recording_version(row["trackName"])), -len(split_credit(row["trackName"])[0])))
            album_ids = []
            for row in anchors[:2]:
                title = split_credit(row["trackName"])[0]
                data = self._json("https://music.163.com/api/search/get", headers={"Referer": "https://music.163.com/"},
                                  s=title, type=1, limit=20)
                songs = data.get("result", {}).get("songs", []) if isinstance(data, dict) else []
                expected = next((t for _, t in self._apple_tracks(dict(results=[row]))), None)
                if not isinstance(songs, list) or expected is None:
                    continue
                for song in songs[:20]:
                    if not isinstance(song, dict):
                        continue
                    candidate, _ = self._netease_record(song)
                    album = song.get("album", song.get("al", {}))
                    source_id = album.get("id") if isinstance(album, dict) else None
                    if (candidate and metadata_rank(candidate, [expected], tolerance=1.5) is not None
                            and type(source_id) is int and 0 < source_id < 10 ** 15 and source_id not in album_ids):
                        album_ids.append(source_id)
            album_ids.extend(key for key, _ in sorted(self.candidate_albums.items(), key=lambda item: item[1])
                             if key not in album_ids)
            verified = []
            for source_id in album_ids[:3]:
                try:
                    data = self._json(f"https://music.163.com/api/album/{source_id}", id=source_id,
                                      headers={"Referer": "https://music.163.com/"})
                    album = data.get("album") if isinstance(data, dict) and data.get("code") == 200 else None
                    if not isinstance(album, dict) or type(album.get("id")) is not int or album["id"] != source_id:
                        continue
                    songs = data.get("songs", album.get("songs"))
                    if not isinstance(songs, list):
                        continue
                    aligned = self._align_album(apple, songs)
                    if aligned:
                        verified.append(aligned)
                except (requests.RequestException, ValueError, TypeError, KeyError):
                    self.failures.add("Album identity")
            if not verified:
                return aliases
            signatures = {tuple(tuple(sorted(self._artist_id("netease", record) or 0 for record in records))
                                for _, _, records in alignment) for alignment in verified}
            if len(signatures) != 1:
                self.uncertain_found = True
                return aliases
            pairs = verified[0]
            if len(self.album_cache) >= 16:
                self.album_cache.pop(next(iter(self.album_cache)))
            self.album_cache[key] = pairs
            # Learn a solo primary artist only when one source identity is common to every album track.
            leads = {normalize(row["artistName"]) for row, _, _ in pairs}
            common = set.intersection(*({self._artist_id("netease", record) for record in records}
                                        for _, _, records in pairs)) - {None}
            if len(leads) == len(common) == 1 and all(len(artist_parts(row["artistName"])) == 1 for row, _, _ in pairs):
                identity = next(iter(common))
                names = {normalize(record["name"]) for _, _, records in pairs for record in records
                         if self._artist_id("netease", record) == identity}
                if len(self.artist_cache) >= 256:
                    self.artist_cache.pop(next(iter(self.artist_cache)))
                self.artist_cache["netease", identity] = frozenset(names | leads) | self.artist_cache.get(("netease", identity), frozenset())
            LOG.info("Album identity verified: apple=%s tracks=%s", album_id, len(pairs))
        translated = next((source for row, source, _ in pairs if row["trackId"] == identifier), None)
        if translated is not None and abs(translated.duration - track.duration) <= 1.5 and translated not in aliases:
            aliases = [*aliases, translated]
            if len(self.alias_cache) >= 64:
                self.alias_cache.pop(next(iter(self.alias_cache)))
            self.alias_cache[track, storefront] = aliases
        return aliases

    @staticmethod
    def _search_queries(aliases):
        queries = [_simplified.convert(f"{track.artist} {split_credit(track.title)[0]}").strip() for track in aliases]
        titles = [_simplified.convert(split_credit(track.title)[0]).strip() for track in aliases]
        return list(dict.fromkeys(queries[:3] + titles[:3]))

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
        if key in self.artist_cache or key in self.artist_round_cache:
            names = self.artist_cache.get(key, self.artist_round_cache.get(key))
            if names & anchor:
                return names
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
            names = frozenset(names)
            self.artist_round_cache[key] = names
            if names - anchor:
                if len(self.artist_cache) >= 256:
                    self.artist_cache.pop(next(iter(self.artist_cache)))
                self.artist_cache[key] = names
            LOG.info("Artist aliases verified: provider=%s artist_id=%s names=%s", provider, identifier, len(names))
            return names
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            self.failures.add(provider + " artist profile")
            LOG.info("Artist profile unavailable: provider=%s error=%s", provider, type(error).__name__)
            return None

    def _source_matches(self, provider, matches, aliases):
        rosters = recording_rosters(aliases)
        known = set().union(*(part for roster in rosters for part in roster))
        known_guests = set().union(*(part for alias in aliases for part in artist_parts(split_credit(alias.title)[1]))) or known
        weak_titles = set()
        for _, _, candidate, _ in matches:
            facts = recording_evidence(candidate, aliases)
            if (not facts.conflict and not facts.title and facts.fragment and facts.album and facts.gap <= .5
                    and any(same_artist_groups(artist_parts(candidate.artist + " & " + split_credit(candidate.title)[1]), roster)
                            for roster in rosters)):
                weak_titles.add(title_name(candidate.title))
        # Verify and download lazily: a slow later profile must not discard an earlier valid song.
        ordered = sorted(matches, key=lambda item: (match_rank(item[2], aliases) is None,
            not any(same_artist_groups(artist_parts(item[2].artist + " & " + split_credit(item[2].title)[1]), roster)
                    for roster in rosters), item[0]))
        for rank, identifier, candidate, records in ordered[:6]:
            self._check()
            facts = recording_evidence(candidate, aliases)
            if facts.conflict or not (facts.title or facts.fragment and facts.album and facts.gap <= .5):
                continue
            if facts.gap > 1.5:
                self.uncertain_found = True
                continue
            if not facts.title and len(weak_titles) > 1:
                self.uncertain_found = True
                continue
            self.uncertain_found |= match_rank(candidate, aliases) is None
            groups, identities = [], []
            for record in records:
                identity = self._artist_id(provider, record)
                names = {normalize(name) for value in catalog_names(record)
                         for name in (value, *bilingual_parts(value)) if normalize(name)}
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
            else:
                self.uncertain_found = True

    def _netease(self, aliases, tolerance=5):
        headers = {"Referer": "https://music.163.com/"}
        valid_query = False
        rejected_query = False
        for query in self._search_queries(aliases):
            try:
                data = self._json("https://music.163.com/api/search/get", headers=headers, s=query, type=1, limit=20)
                result = data.get("result") if isinstance(data, dict) else None
                if not isinstance(data, dict) or data.get("code") != 200 or not isinstance(result, dict):
                    rejected_query = True
                    LOG.info("Lyric service unavailable: provider=NetEase code=%s", data.get("code") if isinstance(data, dict) else None)
                    if isinstance(data, dict) and data.get("code") in (301, -462):
                        self.failures.add("网易云音乐")
                        return None
                    continue
                songs = result.get("songs", [])
                if not isinstance(songs, list):
                    rejected_query = True
                    continue
                valid_query = True
                self.reachable = True
                matches = []
                for item in songs[:50]:
                    if not isinstance(item, dict):
                        continue
                    candidate, records = self._netease_record(item)
                    if candidate is None:
                        continue
                    album = item.get("album", item.get("al", {}))
                    album_id = album.get("id")
                    difference = min(abs(candidate.duration - alias.duration) for alias in aliases)
                    if (type(album_id) is int and 0 < album_id < 10 ** 15 and difference <= 1.5
                            and (album_id in self.candidate_albums or len(self.candidate_albums) < 12)):
                        self.candidate_albums[album_id] = min(difference, self.candidate_albums.get(album_id, difference))
                    rank = metadata_rank(candidate, aliases, tolerance)
                    identifier = item.get("id")
                    if difference <= tolerance and type(identifier) is int and 0 < identifier < 10 ** 15:
                        self._remember("netease", identifier, candidate, records, item)
                        matches.append((rank if rank is not None else (True, difference), identifier, candidate, records))
                LOG.info("Lyric search: provider=NetEase candidates=%s recordings=%s", len(songs), len(matches))
                for _, identifier, candidate in islice(self._source_matches("netease", matches, aliases), 2):
                    result = self._download(LyricCandidate("netease", identifier, candidate, (), {}))
                    if result:
                        return result
            except (requests.RequestException, ValueError, TypeError, KeyError):
                self.failures.add("网易云音乐")
        if rejected_query and not valid_query:
            self.failures.add("网易云音乐")
        return None

    def _qq(self, aliases, tolerance=5):
        headers = {"Referer": "https://y.qq.com/"}
        valid_query = False
        rejected_query = False
        for query in self._search_queries(aliases):
            try:
                body = dict(comm=dict(ct="19", cv="1859", uin="0"), req=dict(method="DoSearchForQQMusicDesktop",
                    module="music.search.SearchCgiService", param=dict(grp=1, num_per_page=20, page_num=1, query=query, search_type=0)))
                data = self._json("https://u.y.qq.com/cgi-bin/musicu.fcg", headers=headers, body=body)
                request = data.get("req") if isinstance(data, dict) else None
                if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(request, dict) or request.get("code") != 0:
                    rejected_query = True
                    LOG.info("Lyric service unavailable: provider=QQMusic code=%s", request.get("code") if isinstance(request, dict) else None)
                    continue
                container = request
                for key in ("data", "body", "song"):
                    container = container.get(key) if isinstance(container, dict) else None
                songs = container.get("list") if isinstance(container, dict) else None
                if not isinstance(songs, list):
                    rejected_query = True
                    continue
                valid_query = True
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
                    candidate = Track(str(item.get("title") or item.get("name") or ""), " & ".join(names), str(album.get("name") or ""),
                                      finite(item.get("interval")), catalog_names(item))
                    rank = metadata_rank(candidate, aliases, tolerance)
                    mid = item.get("mid")
                    difference = min(abs(candidate.duration - alias.duration) for alias in aliases)
                    if difference <= tolerance and isinstance(mid, str) and re.fullmatch(r"[A-Za-z0-9]{1,64}", mid):
                        self._remember("qq", mid, candidate, item["singer"], item)
                        matches.append((rank if rank is not None else (True, difference), mid, candidate, item["singer"]))
                LOG.info("Lyric search: provider=QQMusic candidates=%s recordings=%s", len(songs), len(matches))
                for _, mid, candidate in islice(self._source_matches("qq", matches, aliases), 2):
                    result = self._download(LyricCandidate("qq", mid, candidate, (), {}))
                    if result:
                        return result
            except (requests.RequestException, ValueError, TypeError, KeyError):
                self.failures.add("QQ音乐")
        if rejected_query and not valid_query:
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
        started = time.monotonic()
        resume = (not force and self.lookup_key == (track, storefront) and self.retryable
                  and started - self.lookup_started < 90)
        if not resume:
            self.round_cache.clear()
            self.lookup_started = started
        self.lookup_key = track, storefront
        self.cancelled, self.deadline, self.reachable = cancelled, started + 30, False
        self.current_track, self.current_storefront = track, storefront
        self.candidates.clear()
        self.attempted_lyrics.clear()
        self.request_count = 0
        self.selected_candidate = None
        self.retryable = True
        self.failures = set()
        self.candidate_albums.clear()
        self.plain_found = self.uncertain_found = False
        self.profile_requests = 0
        self.artist_round_cache.clear()
        self._check()
        order = normalized_source_order(source_order)
        key = track, storefront, order
        alias_key = track, storefront
        aliases = self.alias_cache.get(alias_key, [track])
        def finish(document, message, outcome):
            self.retryable = document is None and bool(self.failures)
            decisions = []
            for item in self.candidates.values():
                facts = recording_evidence(item.track, aliases)
                reason = ("recording_conflict" if facts.conflict else "title_unverified" if not facts.title and not facts.fragment
                          else "duration_unverified" if facts.gap > 1.5
                          else "no_timed_lyrics" if (item.provider, item.identifier) in self.attempted_lyrics
                          else "credits_or_release_unverified")
                if self.selected_candidate and (item.provider, item.identifier) == (
                        self.selected_candidate.provider, self.selected_candidate.identifier):
                    reason = "selected"
                decisions.append(dict(source=item.provider, title=item.track.title, artist=item.track.artist, reason=reason))
            chosen = self.selected_candidate
            selected = (dict(source=chosen.provider, identifier=chosen.identifier, title=chosen.track.title,
                             artist=chosen.track.artist, album=chosen.track.album, duration=chosen.track.duration,
                             proof=chosen.proof or ("verified_title_full_credits_duration" if recording_evidence(chosen.track, aliases).title
                                                   else "bilingual_fragment_album_full_credits_precise_duration")) if chosen else None)
            self.diagnostics = dict(outcome=outcome, selected=selected, requests=self.request_count, candidates=len(self.candidates),
                                    elapsed=round(time.monotonic() - started, 3), retryable=self.retryable,
                                    incomplete=sorted(self.failures), decisions=decisions)
            LOG.info("Lyric decision: %s", json.dumps(self.diagnostics, ensure_ascii=False))
            return document, message
        if not force and key in self.cache:
            cached = self.cache[key]
            return finish(cached, f"已匹配 · {cached.source} · {len(cached.lines)} 行（缓存）", "cached")
        if not force:
            cached = self._cached_lyrics(track, storefront, order)
            self._check()
            if cached is not None:
                if len(self.cache) >= 64:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = cached
                return finish(cached, f"已匹配 · {cached.source} · {len(cached.lines)} 行（缓存）", "disk_cached")
        if force:
            self.alias_cache.pop(alias_key, None)
            self.artist_cache.clear()
        self.cache.pop(key, None)
        try:
            self._check()
            base_aliases = [track]
            aliases = self.alias_cache.get(alias_key, base_aliases)
            providers = tuple(getattr(self, "_" + source) for source in order)
            result = self._run_stage(providers[0], aliases, budget=3.)
            if result is None:
                aliases = self._run_stage(self._aliases, track, storefront, budget=3.) or self.alias_cache.get(alias_key, aliases)
                result = self._run_stage(self._resolve_candidates, aliases, order, budget=2.)
                if result is None:
                    result = self._search_sources(aliases, providers if aliases != base_aliases else providers[1:], reserve=12.)
            if result is None:
                expanded = self._run_stage(self._album_aliases, track, storefront, aliases, budget=5.) or aliases
                if expanded != aliases:
                    aliases = expanded
                    result = self._run_stage(self._resolve_candidates, aliases, order, budget=2.)
                    if result is None:
                        result = self._search_sources(aliases, providers, reserve=5.)
            if result is None:
                plausible = any(not recording_evidence(item.track, aliases).conflict
                                and recording_evidence(item.track, aliases).fragment
                                and recording_evidence(item.track, aliases).album for item in self.candidates.values())
                if plausible and alias_key not in self.apple_records:
                    try:
                        self._run_stage(self._artist_catalog, track, storefront, budget=4.)
                    except (requests.RequestException, ValueError, TypeError, KeyError):
                        self.failures.add("Apple artist catalog")
                result = self._run_stage(self._resolve_candidates, aliases, order, deep=True, budget=5.)
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
                reply = finish(result, f"已匹配 · {result.source} · {len(result.lines)} 行", "matched")
                self._cache_lyrics(track, storefront, order, result, self.diagnostics["selected"])
                return reply
            if self.failures:
                return finish(None, "部分曲目或歌词查询暂未完成，可重新匹配", "incomplete")
            if self.plain_found:
                return finish(None, "已找到歌词，但没有时间轴，无法同步滚动", "untimed")
            if self.uncertain_found:
                return finish(None, "候选歌手署名或发行版本尚未核实，未自动采用", "unverified")
            return finish(None, "未找到可靠的同步歌词" if self.reachable else "歌词服务暂时不可用，可点击重新匹配", "not_found")
        except TimeoutError as exc:
            self.failures.add("Search timeout")
            return finish(None, str(exc), "incomplete")


def lyrics_probe(values, report_path, album="", cache_dir=None):
    import json
    from pathlib import Path
    finder = LyricsFinder(cache_dir=cache_dir)
    try:
        track = Track(values[0], values[1], album, duration=float(values[2]))
        result, message = finder.find(track)
        report = dict(ok=result is not None, version=__version__, message=message, source=result.source if result else None,
                      title=result.title if result else None, artist=result.artist if result else None,
                      lines=len(result.lines) if result else 0,
                      first_time=result.lines[0].time if result else None,
                      last_time=result.lines[-1].time if result else None, diagnostics=finder.diagnostics)
        if report_path:
            Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True), flush=True)
        return 0 if result else 1
    finally:
        finder.close()
