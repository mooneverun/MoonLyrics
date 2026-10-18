"""One cancellable search worker; only the latest requested song reaches the UI."""
import logging
import threading

from PyQt6.QtCore import QThread, pyqtSignal

from .lyrics import LyricsFinder, SearchCancelled

LOG = logging.getLogger("moonlyrics.lyrics")


class LyricsWorker(QThread):
    result_ready = pyqtSignal(int, object, str)

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
                self.wake.wait(); self.wake.clear()
                with self.lock:
                    job, self.pending = self.pending, None
                if job is None:
                    continue
                request_id, track, storefront, force = job
                cancelled = lambda: self.isInterruptionRequested() or request_id != self.latest
                try:
                    document, message = finder.find(track, storefront, cancelled, force)
                    if not cancelled():
                        self.result_ready.emit(request_id, document, message)
                except SearchCancelled:
                    continue
                except Exception:
                    LOG.exception("Lyric search failed")
                    if not cancelled():
                        self.result_ready.emit(request_id, None, "歌词匹配失败，可点击重新匹配")
        finally:
            finder.close()
