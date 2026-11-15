"""Visible-only settings backdrop; rendering is shared with the lyric surface."""
import logging
import time

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer

from .windows_capture import DesktopRegion

LOG = logging.getLogger("moonlyrics")


class SettingsBackdrop(QObject):
    def __init__(self, owner, view_factory, settings, shared_with):
        super().__init__(owner)
        self.owner = owner
        self.settings = settings
        self.capture = DesktopRegion(shared_with=shared_with)
        self.view = view_factory(settings, self.capture, owner, settings_surface=True)
        self.view.setGeometry(owner.rect())
        self.view.lower()
        self.view.failed.connect(self.renderer_failed)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.refresh)
        self.move_timer = QTimer(self)
        self.move_timer.setSingleShot(True)
        self.move_timer.timeout.connect(lambda: self.refresh(moving=True))
        self.settle_timer = QTimer(self)
        self.settle_timer.setSingleShot(True)
        self.settle_timer.timeout.connect(self.settled)
        self.fallback_timer=QTimer(self)
        self.fallback_timer.setSingleShot(True)
        self.fallback_timer.timeout.connect(owner.restore_plain_surface)
        self.release_timer = QTimer(self)
        self.release_timer.setSingleShot(True)
        self.release_timer.setInterval(3000)
        self.release_timer.timeout.connect(self.release_hidden_surface)
        self.changed_at = time.monotonic()
        self.retry_delay = 0
        self.needs_reset = False
        self.failed = False
        self.released = False
        owner.installEventFilter(self)

    def release_hidden_surface(self):
        if self.owner.isVisible() or self.released or self.failed:
            return
        self.stop()
        self.fallback_timer.stop()
        self.owner.backdrop_active = False
        self.view.release_material_frame()
        self.released = True

    def interval(self):
        moving = self.owner.backdrop_dragging or time.monotonic()-self.changed_at < .75
        return round(1000/min(self.settings["fps"], 30 if moving else 5))

    def start(self):
        if self.failed or not self.owner.isVisible() or self.owner.isMinimized():
            return
        self.release_timer.stop()
        self.released = False
        self.view.setGeometry(self.owner.rect())
        self.capture.shared_with.excluded_windows=(int(self.owner.glass.winId()),int(self.owner.winId()),int(self.owner.runtime_menu.winId()))
        self.view.lower()
        self.refresh()
        self.timer.start(self.retry_delay or self.interval())

    def stop(self):
        self.timer.stop(); self.move_timer.stop(); self.settle_timer.stop()
        self.owner.backdrop_dragging = False
        # Keep the GPU frame for quick reopening. Native pixels are released
        # immediately; the delayed release also drops the panel's GPU frames.
        self.view.new_frame = False
        self.capture.close()
        self.needs_reset = True

    def refresh(self, *, moving=False):
        if self.failed or not self.owner.isVisible() or self.owner.isMinimized():
            return
        try:
            if self.needs_reset:
                self.capture.reset()
                self.needs_reset = False
            fresh = self.view.take_frame(refresh=not moving)
            if self.view.background_changed:
                self.changed_at = time.monotonic()
        except OSError:
            self.view.new_frame = False
            self.needs_reset = True
            if not self.retry_delay:
                LOG.exception("Settings backdrop unavailable; retrying separately from the lyric bar")
            self.retry_delay = min(5000, self.retry_delay*2 or 1000)
            self.timer.start(self.retry_delay)
            return
        except Exception as error:
            self.renderer_failed(str(error))
            return
        self.retry_delay = 0
        if fresh and self.view.ready and not self.owner.backdrop_active:
            self.owner.backdrop_active = True
            self.owner.update()
        if not moving:
            interval = self.interval()
            if not self.timer.isActive() or self.timer.interval() != interval:
                self.timer.start(interval)

    def settled(self):
        self.owner.backdrop_dragging = False
        self.refresh()

    def renderer_failed(self, message):
        LOG.error("Settings glass unavailable; using the readable theme fallback: %s", message)
        self.failed = True
        self.stop()
        self.owner.backdrop_active = False
        # A rendering failure may arrive from paintGL itself. Move the
        # controls back to the plain surface after that paint has returned.
        self.fallback_timer.start(0)
        self.owner.update()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.Show:
            self.release_timer.stop()
            QTimer.singleShot(0, self.start)
        elif kind == QEvent.Type.Hide:
            self.stop()
            if not getattr(self.owner,'closing',False):self.release_timer.start()
        elif kind == QEvent.Type.DeferredDelete:
            self.shutdown()
        elif kind == QEvent.Type.Resize:
            self.view.setGeometry(self.owner.rect())
            self.changed_at = time.monotonic()
            self.move_timer.start(0)
        elif kind == QEvent.Type.Move and self.owner.isVisible():
            self.owner.backdrop_dragging = True
            self.changed_at = time.monotonic()
            self.settle_timer.start(160)
            self.move_timer.start(0)
        return super().eventFilter(watched, event)

    def shutdown(self):
        self.stop()
        self.fallback_timer.stop()
        self.release_timer.stop()
        self.view.shutdown()
