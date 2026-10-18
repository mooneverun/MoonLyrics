"""Read Apple Music's Windows media session without blocking the Qt UI."""
from datetime import datetime, timezone
import json
import logging
import threading
import time

from PyQt6.QtCore import QThread, QTimer, pyqtSignal

from .playback import PlaybackSnapshot, apple_metadata, finite, timeline_anchor

LOG = logging.getLogger("moonlyrics.media")


def select_apple_session(sessions):
    candidates = []
    for session in sessions:
        try:
            if "applemusic" in session.source_app_user_model_id.casefold():
                candidates.append(session)
        except OSError:
            continue
    for session in candidates:
        try:
            if int(session.get_playback_info().playback_status) == 4:
                return session
        except OSError:
            continue
    return candidates[0] if candidates else None


class AppleMusicReader(QThread):
    snapshot_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wake = threading.Event()
        self._tokens = []
        self._session = None
        self._manager = None
        self._revision = 0

    def stop(self):
        self.requestInterruption()
        self.wake.set()

    def _changed(self, *args):
        self._revision += 1
        self.wake.set()

    def _subscribe(self, owner, names):
        for name in names:
            token = getattr(owner, "add_" + name)(self._changed)
            self._tokens.append((owner, "remove_" + name, token))

    def _unsubscribe(self, owner=None):
        kept = []
        for target, method, token in self._tokens:
            if owner is not None and target is not owner:
                kept.append((target, method, token))
                continue
            try:
                getattr(target, method)(token)
            except OSError:
                LOG.debug("Media event was already disconnected", exc_info=True)
        self._tokens = kept

    def _get(self, operation):
        from winrt.windows.foundation import AsyncStatus
        deadline = time.perf_counter() + 2.
        while operation.wait(.1) == AsyncStatus.STARTED:
            if self.isInterruptionRequested() or time.perf_counter() >= deadline:
                operation.cancel()
                if self.isInterruptionRequested():
                    raise InterruptedError()
                raise TimeoutError("Apple Music did not respond in time")
        if self.isInterruptionRequested():
            raise InterruptedError()
        return operation.get()

    def _read(self, session):
        properties = self._get(session.try_get_media_properties_async())
        timeline = session.get_timeline_properties()
        playback = session.get_playback_info()
        now = time.perf_counter()
        status = {0: "closed", 1: "opened", 2: "changing", 3: "stopped",
                  4: "playing", 5: "paused"}.get(int(playback.playback_status), "opened")
        title, artist, album = apple_metadata(properties.title, properties.artist, properties.album_title)
        rate = finite(playback.playback_rate, 1.)
        rate = rate if 0 < rate <= 16 else 1.
        try:
            updated = timeline.last_updated_time.timestamp()
        except (AttributeError, ValueError, OSError, OverflowError):
            updated = float("inf")
        position, duration = timeline_anchor(timeline.position.total_seconds(),
                                             timeline.start_time.total_seconds(),
                                             timeline.end_time.total_seconds(), updated,
                                             datetime.now(timezone.utc).timestamp(), status, rate)
        return PlaybackSnapshot(source=session.source_app_user_model_id, title=title, artist=artist,
                                album=album, status=status, position=position, duration=duration,
                                rate=rate, observed_at=now)

    def run(self):
        try:
            # Explicit imports also make the generic WinRT projections available in the frozen app.
            import winrt.windows.foundation.collections  # noqa: F401
            from winrt.runtime import ApartmentType, init_apartment
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
            init_apartment(ApartmentType.MULTI_THREADED)
            previous_error = None
            while not self.isInterruptionRequested():
                self.wake.clear()
                try:
                    if self._manager is None:
                        self._manager = self._get(GlobalSystemMediaTransportControlsSessionManager.request_async())
                        self._subscribe(self._manager, ("sessions_changed",))
                    session = select_apple_session(self._manager.get_sessions())
                    if session != self._session:
                        if self._session is not None:
                            self._unsubscribe(self._session)
                        self._session = session
                        if session is not None:
                            self._subscribe(session, ("media_properties_changed", "playback_info_changed",
                                                      "timeline_properties_changed"))
                    revision = self._revision
                    snapshot = self._read(session) if session is not None else PlaybackSnapshot()
                    if revision != self._revision:
                        continue  # A seek or track change arrived while the properties were being read.
                    if not self.isInterruptionRequested():
                        self.snapshot_changed.emit(snapshot)
                    previous_error = None
                except InterruptedError:
                    break
                except Exception as exc:
                    if type(exc).__name__ != previous_error:
                        LOG.warning("Apple Music read failed: %s", exc, exc_info=True)
                    previous_error = type(exc).__name__
                    self.snapshot_changed.emit(PlaybackSnapshot(status="unavailable", message=str(exc)))
                    self._unsubscribe(); self._session = None; self._manager = None
                # Coalesce bursts of native events, with a half-second heartbeat for missed updates.
                self.wake.wait(.5)
                if self.wake.is_set():
                    time.sleep(.05)
        except Exception as exc:
            LOG.exception("Windows media reader could not start")
            self.snapshot_changed.emit(PlaybackSnapshot(status="unavailable", message=str(exc)))
        finally:
            self._unsubscribe(); self._session = None; self._manager = None


def media_probe(app, report_path):
    """Short, read-only packaged-runtime check; it never controls playback."""
    from dataclasses import asdict
    samples = []
    reader = AppleMusicReader()
    reader.snapshot_changed.connect(samples.append)
    reader.start()
    QTimer.singleShot(5500, app.quit)
    app.exec()
    reader.stop()
    stopped = reader.wait(2500)
    detected = [sample for sample in samples if sample.source]
    advanced = any(b.position > a.position + .05 for a, b in zip(detected, detected[1:])
                   if (a.title, a.artist) == (b.title, b.artist) and a.status == b.status == "playing")
    result = dict(ok=stopped and bool(samples) and not any(s.status == "unavailable" for s in samples),
                  apple_music_detected=bool(detected), updates=len(samples), timeline_advanced=advanced,
                  last_snapshot=asdict(samples[-1]) if samples else None)
    if report_path:
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result["ok"] else 1
