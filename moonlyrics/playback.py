"""Playback values and a monotonic display clock, shared by platform readers."""
from dataclasses import dataclass
import math
import time


def finite(value, default=0.):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def timeline_anchor(position, start, end, updated_at, wall_now, status, rate):
    start = finite(start)
    duration = max(0., finite(end) - start)
    position = max(0., finite(position) - start)
    age = finite(wall_now) - finite(updated_at, float("inf"))
    # A future or stale timestamp is not a reliable basis for extrapolation.
    if status == "playing" and 0 <= age <= 5:
        position += age * rate
    return (min(position, duration) if duration else position), duration


def apple_metadata(title, artist, album):
    title, artist, album = (str(value or "").strip() for value in (title, artist, album))
    # Apple Music may combine artist and album in the artist field.
    name, separator, reported_album = artist.rpartition(" — ")
    if separator and name.strip() and (not album or album == reported_album.strip()):
        artist, album = name.strip(), album or reported_album.strip()
    return title, artist, album


@dataclass(frozen=True)
class PlaybackSnapshot:
    source: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    status: str = "waiting"
    position: float = 0.
    duration: float = 0.
    rate: float = 1.
    observed_at: float = 0.
    message: str = ""
    controls: tuple = ()

    def position_at(self, now=None):
        now = time.perf_counter() if now is None else now
        position = self.position
        if self.status == "playing" and self.title:
            # The reader normally refreshes at least twice a second. Freeze a lost feed.
            position += min(3., max(0., now - self.observed_at)) * self.rate
        return max(0., min(position, self.duration) if self.duration else position)


class LyricClock:
    """Absorb small media-clock corrections without rewinding the lyrics."""
    def __init__(self):
        self.snapshot = None
        self.anchor = self.observed_at = self.correction = 0.

    def update(self, snapshot, now=None):
        now = time.perf_counter() if now is None else now
        previous = self.snapshot
        estimated, target = self.position_at(now), snapshot.position_at(now)
        identity = lambda s: (s.source, s.title, s.artist, s.album)
        reset = (previous is None or identity(previous) != identity(snapshot)
                 or previous.status != snapshot.status or previous.rate != snapshot.rate
                 or snapshot.status != "playing" or abs(target - estimated) > 1.)
        self.snapshot, self.observed_at = snapshot, now
        self.anchor = target if reset else estimated
        self.correction = target - self.anchor

    def position_at(self, now=None):
        if self.snapshot is None:
            return 0.
        now = time.perf_counter() if now is None else now
        position = self.anchor
        if self.snapshot.status == "playing" and self.snapshot.title:
            elapsed = min(3., max(0., now - self.observed_at)) * self.snapshot.rate
            # Slew at up to 15% of playback speed; a small correction never runs backwards.
            position += elapsed + max(-elapsed * .15, min(elapsed * .15, self.correction))
        duration = self.snapshot.duration
        return max(0., min(position, duration) if duration else position)


def format_time(seconds):
    seconds = int(max(0., finite(seconds)))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


STATUS_LABELS = dict(waiting="等待 Apple Music", playing="正在播放", paused="已暂停",
                     stopped="已停止", changing="正在切换歌曲", opened="等待播放",
                     closed="Apple Music 已关闭", unavailable="暂时无法读取，正在重试")
