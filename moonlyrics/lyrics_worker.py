"""One cancellable search worker; only the latest requested song reaches the UI."""
import logging
import threading

from PyQt6.QtCore import QThread, pyqtSignal

from .lyrics import LyricsFinder, SearchCancelled

LOG = logging.getLogger("moonlyrics.lyrics")


class LyricsWorker(QThread):
    result_ready = pyqtSignal(int, object, str)
    retry_delays = (2., 6.)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.latest = 0
        self.pending = None

    def request(self, request_id, track, storefront, force=False):
        with self.lock:
            self.latest = request_id
            self.pending = (request_id, track, storefront, force) if track else None
            self.wake.set()

    def stop(self):
        self.requestInterruption(); self.wake.set()

    def run(self):
        finder = LyricsFinder()
        try:
            while not self.isInterruptionRequested():
                self.wake.wait()
                with self.lock:
                    job, self.pending = self.pending, None
                    self.wake.clear()
                if job is None:
                    continue
                request_id, track, storefront, force = job
                cancelled = lambda: self.isInterruptionRequested() or request_id != self.latest
                LOG.info("Lyric match started: request=%s duration=%.2f", request_id, track.duration)
                for attempt, delay in enumerate((0.,) + self.retry_delays):
                    # A new song, manual retry or shutdown wakes this wait immediately.
                    if (delay and self.wake.wait(delay)) or cancelled():
                        break
                    try:
                        document, message = finder.find(track, storefront, cancelled, force or attempt > 0)
                    except SearchCancelled:
                        break
                    except Exception:
                        LOG.exception("Lyric search failed")
                        document, message = None, "歌词匹配失败，可点击重新匹配"
                    if cancelled():
                        break
                    LOG.info("Lyric match result: request=%s attempt=%s found=%s source=%s lines=%s message=%s",
                             request_id, attempt + 1, document is not None, document.source if document else "",
                             len(document.lines) if document else 0, message)
                    if document is not None or attempt == len(self.retry_delays):
                        if document is None and attempt:
                            message += f"（已重试 {attempt} 次）"
                        self.result_ready.emit(request_id, document, message)
                        break
                    self.result_ready.emit(request_id, None,
                                           f"{message}；{self.retry_delays[attempt]:g} 秒后自动重试（{attempt + 1}/{len(self.retry_delays)}）")
        finally:
            finder.close()
