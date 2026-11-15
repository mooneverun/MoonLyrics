"""MoonLyrics Windows desktop glass, playback integration and synchronized lyrics."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import logging
import math
import os
from pathlib import Path
import statistics
import struct
import sys
import time
import winreg

from PyQt6.QtCore import QEvent, QEasingCurve, QPoint, QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QPainter, QSurfaceFormat, QVector2D, QVector4D
from PyQt6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                           QOpenGLShader, QOpenGLShaderProgram,
                           QOpenGLTimerQuery, QOpenGLVertexArrayObject)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import (QApplication, QComboBox, QFontComboBox, QGraphicsOpacityEffect, QGridLayout,
                            QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
                            QSizePolicy, QSlider, QToolButton, QToolTip, QVBoxLayout, QWidget)

from .styles import DEFAULTS, LIMITS, PRESETS, APPEARANCE_MODES, LYRIC_SOURCES, FPS_OPTIONS, normalized, lyric_offset_seconds
from .glass_material import GLASS, LENS, background_tone, sample_background
from . import __version__
from .i18n import LANGUAGES, TEXTS, translate, translate_message
from .playback import LyricClock, PlaybackSnapshot, STATUS_LABELS, format_time
from .windows_media import AppleMusicReader, media_probe
from .lyrics import Lyrics, LyricLine, Track, lyrics_probe, normalize
from .lyrics_worker import LyricsWorker
from .lyric_paint import load_bundled_font, lyric_font, lyric_layer, lyric_pan
from .settings_ui import SettingsWindow, application_icon, media_icon
from .settings_content import build_settings, SHORT_MODES, POSITION_LABELS
from .settings_icons import settings_icon
from .windows_capture import DesktopRegion, api, client_origin
from .storage import application_directory, data_directory

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "MoonLyrics"
PREFERRED_APP_DIR = APP_DIR
LEGACY_DIR = APP_DIR.parent / "LyricFloatGlassSample"
LOG = logging.getLogger("moonlyrics")
user32, kernel32, psapi = (C.WinDLL(n, use_last_error=True) for n in ("user32", "kernel32", "psapi"))


set_affinity = api(user32, "SetWindowDisplayAffinity", W.BOOL, W.HWND, W.DWORD)
get_affinity = api(user32, "GetWindowDisplayAffinity", W.BOOL, W.HWND, C.POINTER(W.DWORD))
get_window_long = api(user32, "GetWindowLongPtrW", C.c_ssize_t, W.HWND, C.c_int)
get_window = api(user32, "GetWindow", W.HWND, W.HWND, W.UINT)
window_attribute = api(C.WinDLL("dwmapi"), "DwmGetWindowAttribute", C.c_long,
                       W.HWND, W.DWORD, C.c_void_p, W.DWORD)
set_window_pos = api(user32, "SetWindowPos", W.BOOL, W.HWND, W.HWND,
                     C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
class MemoryCounters(C.Structure):
    _fields_ = [("cb", W.DWORD), ("page_faults", W.DWORD)] + [
        (key, C.c_size_t) for key in ("peak_working_set", "working_set", "peak_paged",
                                     "paged", "peak_nonpaged", "nonpaged", "pagefile",
                                     "peak_pagefile", "private")]


process_handle = api(kernel32, "GetCurrentProcess", W.HANDLE)()
process_times = api(kernel32, "GetProcessTimes", W.BOOL, W.HANDLE,
                    *([C.POINTER(W.FILETIME)] * 4))
memory_info = api(psapi, "GetProcessMemoryInfo", W.BOOL, W.HANDLE,
                  C.POINTER(MemoryCounters), W.DWORD)


class Usage:
    def __init__(self):
        self.last_wall = time.perf_counter()
        self.last_cpu = self.cpu_time()

    @staticmethod
    def cpu_time():
        values = [W.FILETIME() for _ in range(4)]
        if not process_times(process_handle, *(C.byref(v) for v in values)):
            raise C.WinError(C.get_last_error())
        return sum((v.dwHighDateTime << 32) | v.dwLowDateTime for v in values[2:]) / 1e7

    def read(self):
        now, cpu = time.perf_counter(), self.cpu_time()
        percent = (cpu - self.last_cpu) / max(.001, now - self.last_wall) * 100 / (os.cpu_count() or 1)
        self.last_wall, self.last_cpu = now, cpu
        mem = MemoryCounters()
        mem.cb = C.sizeof(mem)
        if not memory_info(process_handle, C.byref(mem), mem.cb):
            raise C.WinError(C.get_last_error())
        return percent, mem.working_set / (1024 ** 2)


def system_is_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return not bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        return QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


VERTEX = """#version 330 core
layout(location=0) in vec2 aPosition;
layout(location=1) in vec2 aUv;
out vec2 uv;
void main() { uv = aUv; gl_Position = vec4(aPosition,0.0,1.0); }
"""
BLUR = """#version 330 core
in vec2 uv; out vec4 outputColor;
uniform sampler2D uSource;
uniform vec2 uAxis;
uniform float uSigma;
uniform int uRadius;
uniform vec4 uBounds;
uniform float uFlipY;
void main() {
    vec3 sum=vec3(0); float total=0;
    for(int i=-uRadius;i<=uRadius;i++) {
        float x=float(i)/uSigma, weight=exp(-0.5*x*x);
        vec2 center=vec2(uv.x,mix(uv.y,1.-uv.y,uFlipY));
        vec2 p=clamp(center+uAxis*float(i),uBounds.xy,uBounds.zw);
        sum+=texture(uSource,p).rgb*weight; total+=weight;
    }
    outputColor=vec4(sum/total,1);
}
"""
class GlassView(QOpenGLWidget):
    failed = pyqtSignal(str)
    tone_changed = pyqtSignal()

    def __init__(self, settings, capture, parent, *, settings_surface=False):
        super().__init__(parent)
        self.settings, self.capture = settings, capture
        self.settings_surface = settings_surface
        self.corner_radius = 30. if settings_surface else 0.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,not settings_surface)
        self.ready = False
        self.new_frame = False
        self.frame_geometry = None
        self.texture_size = (0, 0)
        self.texture_rect = None
        self.texture_bounds = (0., 0., 1., 1.)
        self.texture_top_down = False
        self.scene_key = None
        self.pixel_key = None
        self.background_changed = False
        self.uploaded_revision = None
        self.blur_key = None
        self.fbos = []
        self.lens_fbo = None
        self.lens_size = (0, 0)
        self.texture = C.c_uint(0)
        self.frames = 0
        self.capture_ms = 0.
        self.gpu_ms = None
        self.gpu_pending = False
        self.show_text = not settings_surface
        self.primary_text = "等待 Apple Music"
        self.secondary_text = "播放音乐后显示歌曲与进度"
        self.lyrics = None
        self.scroll_position = -1.
        self.pan_position = (-1, 0.)
        self.lyric_time = 0.
        self.lyric_end_time = None
        self.control_space = 0
        self.lyric_index = -1
        self.lyric_image_key = None
        self.dark_text = True
        self.night = None
        self.background_colors = ()
        self.tint_alpha = background_tone((), False)
        self.renderer = "正在初始化显卡…"
        self.update_appearance()
        QApplication.styleHints().colorSchemeChanged.connect(self.update_appearance)
        self.frameSwapped.connect(self.swapped)

    def update_appearance(self):
        mode = self.settings["appearance_mode"]
        night = mode == "night" or (mode == "system" and system_is_dark())
        self.set_night(night)

    def material_tone(self, colors, night):
        floor = (.40 if night else .56) if self.settings_surface else 0.
        return max(floor, background_tone(colors, night))

    def set_night(self, night):
        if self.night == night:
            return
        self.night = night
        self.dark_text = not night
        self.tint_alpha = self.material_tone(self.background_colors, night)
        self.lyric_image_key = None
        self.tone_changed.emit()
        self.update()

    def swapped(self):
        self.frames += 1

    def lyric_width(self):
        return max(1, self.width() - self.control_space)

    def set_lyrics(self, document, position=0.):
        self.lyrics = document
        if document is None:
            self.lyric_image = None
        self.lyric_time = position
        self.lyric_end_time = None
        self.lyric_index = document.index_at(position) if document else -1
        self.scroll_position = document.scroll_at(position) if document else -1.
        self.pan_position = lyric_pan(document, position, self.lyric_width(), self.settings["lyric_font"])
        self.lyric_image_key = None
        self.update()

    def set_lyric_time(self, position, end_time=None):
        if self.lyrics is None:
            return
        self.lyric_index = self.lyrics.index_at(position)
        self.lyric_time = position
        self.lyric_end_time = end_time
        scroll = self.lyrics.scroll_at(position)
        pan = lyric_pan(self.lyrics, position, self.lyric_width(), self.settings["lyric_font"], end_time)
        if abs(scroll - self.scroll_position) > 1e-7 or pan != self.pan_position:
            self.scroll_position, self.pan_position = scroll, pan
            self.update()

    def initializeGL(self):
        try:
            self.gl = C.WinDLL("opengl32")
            signatures = {
                "glViewport": (None, C.c_int, C.c_int, C.c_int, C.c_int),
                "glClearColor": (None, C.c_float, C.c_float, C.c_float, C.c_float),
                "glClear": (None, C.c_uint), "glDisable": (None, C.c_uint),
                "glDrawArrays": (None, C.c_uint, C.c_int, C.c_int),
                "glGenTextures": (None, C.c_int, C.POINTER(C.c_uint)),
                "glDeleteTextures": (None, C.c_int, C.POINTER(C.c_uint)),
                "glBindTexture": (None, C.c_uint, C.c_uint),
                "glTexParameteri": (None, C.c_uint, C.c_uint, C.c_int),
                "glTexImage2D": (None, C.c_uint, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_void_p),
                "glTexSubImage2D": (None, C.c_uint, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_void_p),
                "glGetString": (C.c_char_p, C.c_uint),
            }
            for name, signature in signatures.items():
                api(self.gl, name, *signature)
            get_proc = api(self.gl, "wglGetProcAddress", C.c_void_p, C.c_char_p)
            self.bind_fbo = C.WINFUNCTYPE(None, C.c_uint, C.c_uint)(get_proc(b"glBindFramebuffer"))
            self.active_texture = C.WINFUNCTYPE(None, C.c_uint)(get_proc(b"glActiveTexture"))
            self.renderer = self.gl.glGetString(0x1F01).decode("utf-8", errors="replace")
            LOG.info("Renderer: %s; OpenGL: %s", self.renderer, self.gl.glGetString(0x1F02))
            self.blur_program, self.glass_program, self.lens_program = (
                self.program(fragment) for fragment in (BLUR, GLASS, LENS))
            self.vao, self.vbo = QOpenGLVertexArrayObject(self), QOpenGLBuffer()
            if not self.vao.create() or not self.vbo.create():
                raise RuntimeError("无法创建显卡绘制缓冲区")
            self.vao.bind(); self.vbo.bind()
            vertices = (-1., -1., 0., 0., 1., -1., 1., 0., -1., 1., 0., 1.,
                        -1., 1., 0., 1., 1., -1., 1., 0., 1., 1., 1., 1.)
            data = struct.pack("24f", *vertices)
            self.vbo.allocate(data, len(data))
            self.blur_program.bind()
            for slot, offset in ((0, 0), (1, 8)):
                self.blur_program.enableAttributeArray(slot)
                self.blur_program.setAttributeBuffer(slot, 0x1406, offset, 2, 16)
            self.vao.release(); self.vbo.release(); self.blur_program.release()
            self.gl.glGenTextures(1, C.byref(self.texture))
            self.gl.glBindTexture(0x0DE1, self.texture.value)
            for key, value in ((0x2801, 0x2601), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
                self.gl.glTexParameteri(0x0DE1, key, value)
            self.query = QOpenGLTimerQuery(self)
            self.query_enabled = self.query.create()
            self.ready = True
            self.context().aboutToBeDestroyed.connect(self.shutdown, Qt.ConnectionType.DirectConnection)
        except Exception as exc:
            LOG.exception("OpenGL initialization failed")
            QTimer.singleShot(0, lambda message=str(exc): self.failed.emit(message))

    def program(self, fragment):
        program = QOpenGLShaderProgram(self)
        if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERTEX):
            raise RuntimeError(program.log())
        if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, fragment) or not program.link():
            raise RuntimeError(program.log())
        return program

    def take_frame(self, refresh=True):
        started = time.perf_counter()
        self.background_changed = False
        dpr = self.devicePixelRatioF()
        pad = math.ceil(12 * dpr)
        hwnd = int(self.window().winId())
        dragging = (getattr(self.window(), "drag_anchor", None) is not None
                    or getattr(self.window(), "backdrop_dragging", False))
        options = dict(margin=math.ceil(256*dpr)) if dragging else {}
        if not refresh:
            options["refresh"] = False
        geometry = self.capture.capture(hwnd, pad, **options)
        if geometry is None:
            return False
        self.frame_geometry = geometry
        fw, fh, _ = self.frame_geometry
        cw, ch = self.capture.size
        ox, oy = getattr(self.capture, "offset", (pad, pad))
        revision = getattr(self.capture, "revision", None)
        pixel_key = (revision, cw, ch)
        scene_key = (pixel_key, geometry, ox, oy)
        self.background_changed = revision is None or pixel_key != self.pixel_key
        self.capture_ms = self.capture.capture_ms + (time.perf_counter() - started) * 1000
        if revision is not None and scene_key == self.scene_key:
            return True
        pixels = getattr(self.capture, "buffer", None)
        if pixels is None:
            pixels = (C.c_ubyte * (cw*ch*4)).from_address(self.capture.bits.value)
        colors = sample_background(pixels, (cw, ch), (fw, fh), (ox, oy), dpr,
                                   getattr(self.capture, "top_down", False))
        self.background_colors = colors
        self.tint_alpha = self.material_tone(colors, self.night)
        self.capture_ms = self.capture.capture_ms + (time.perf_counter() - started) * 1000
        self.new_frame |= revision is None or revision != self.uploaded_revision
        self.scene_key, self.pixel_key = scene_key, pixel_key
        self.update()
        return True

    def draw(self, program, source, width, height):
        self.gl.glViewport(0, 0, width, height)
        if source is not None:
            self.active_texture(0x84C0)
            self.gl.glBindTexture(0x0DE1, source)
            program.setUniformValue("uSource", 0)
        self.gl.glDrawArrays(0x0004, 0, 6)

    def paintGL(self):
        if not self.ready:
            return
        try:
            self.paint_material()
        except Exception as exc:
            self.ready = False
            LOG.exception("Material rendering failed")
            self.failed.emit(str(exc))

    def paint_material(self):
        self.gl.glDisable(0x0B71); self.gl.glDisable(0x0BE2); self.gl.glDisable(0x0C11)
        self.gl.glClearColor(0., 0., 0., 0.); self.gl.glClear(0x00004000)
        if not self.frame_geometry or (not self.new_frame and not all(self.texture_size)):
            return
        if self.query_enabled and self.gpu_pending and self.query.isResultAvailable():
            self.gpu_ms = self.query.waitForResult() / 1e6
            self.gpu_pending = False
        measure = self.query_enabled and not self.gpu_pending
        if measure:
            self.query.begin()
        self.vao.bind()
        # Keep the last uploaded texture usable while native capture resources are rebuilt.
        cw, ch = self.capture.size if self.new_frame else self.texture_size
        self.active_texture(0x84C0)
        self.gl.glBindTexture(0x0DE1, self.texture.value)
        if self.new_frame:
            if self.texture_size != (cw, ch):
                self.gl.glTexImage2D(0x0DE1, 0, 0x8058, cw, ch, 0, 0x80E1, 0x1401, self.capture.bits)
                self.texture_size = cw, ch
                self.fbos = [QOpenGLFramebufferObject(cw, ch) for _ in range(2)]
                if not all(f.isValid() for f in self.fbos):
                    raise RuntimeError("无法创建模糊缓冲区")
                # Qt's FBO textures default to nearest sampling. Keep both passes continuous.
                for fbo in self.fbos:
                    self.gl.glBindTexture(0x0DE1, fbo.texture())
                    for key, value in ((0x2801, 0x2601), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
                        self.gl.glTexParameteri(0x0DE1, key, value)
            else:
                self.gl.glTexSubImage2D(0x0DE1, 0, 0, 0, cw, ch, 0x80E1, 0x1401, self.capture.bits)
            self.texture_rect = getattr(self.capture, "source_rect", None)
            self.texture_top_down = getattr(self.capture, "top_down", False)
            self.texture_bounds = self.capture.bounds
            self.uploaded_revision = getattr(self.capture, "revision", None)
            self.blur_key = None
            self.new_frame = False
        source = self.texture.value
        bounds = QVector4D(*self.texture_bounds)
        sigma = 2.
        if sigma > .01:
            sigma *= self.devicePixelRatioF()
            if self.blur_key != sigma:
                program = self.blur_program
                program.bind(); program.setUniformValue("uBounds", bounds)
                spacing = max(1., 3 * sigma / 128)
                program.setUniformValue("uSigma", float(sigma / spacing))
                program.setUniformValue("uRadius", min(128, math.ceil(3 * sigma / spacing)))
                for index, fbo in enumerate(self.fbos):
                    fbo.bind()
                    program.setUniformValue("uFlipY", float(index == 0 and self.texture_top_down))
                    program.setUniformValue("uAxis", QVector2D(spacing / cw, 0) if index == 0 else QVector2D(0, spacing / ch))
                    self.draw(program, source, fbo.width(), fbo.height())
                    source = fbo.texture()
                program.release()
                self.blur_key = sigma
            source = self.fbos[-1].texture()
        fw, fh, pad = self.frame_geometry
        size = (max(1, round(fw / self.devicePixelRatioF())),
                max(1, round(fh / self.devicePixelRatioF())))
        rim = max(4, round(min(40., min(size) / 3.5)))
        if self.lens_size != size:
            self.lens_fbo = QOpenGLFramebufferObject(*size)
            if not self.lens_fbo.isValid():
                raise RuntimeError("无法创建显卡绘制缓冲区")
            self.lens_fbo.bind()
            program = self.lens_program
            program.bind()
            program.setUniformValue("uSize", QVector2D(*size))
            program.setUniformValue("uRim", float(rim))
            program.setUniformValue("uCornerRadius", self.corner_radius)
            self.draw(program, None, *size)
            program.release()
            self.active_texture(0x84C1)
            self.gl.glBindTexture(0x0DE1, self.lens_fbo.texture())
            for key, value in ((0x2801, 0x2601), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
                self.gl.glTexParameteri(0x0DE1, key, value)
            self.lens_size = size
        self.bind_fbo(0x8D40, self.defaultFramebufferObject())
        program = self.glass_program
        program.bind()
        program.setUniformValue("uFrameSize", QVector2D(fw, fh))
        program.setUniformValue("uCaptureSize", QVector2D(cw, ch))
        program.setUniformValue("uBounds", bounds)
        sample_origin = QVector2D(pad, pad)
        if self.texture_rect is not None:
            point = W.POINT()
            if not client_origin(int(self.window().winId()), C.byref(point)):
                raise C.WinError(C.get_last_error())
            sample_origin = QVector2D(point.x-self.texture_rect[1], self.texture_rect[2]+ch-point.y-fh)
        program.setUniformValue("uOrigin", sample_origin)
        program.setUniformValue("uDpr", float(self.devicePixelRatioF()))
        program.setUniformValue("uNight", float(self.night))
        program.setUniformValue("uTintAlpha", float(self.tint_alpha))
        program.setUniformValue("uRim", float(rim))
        program.setUniformValue("uCornerRadius", self.corner_radius)
        self.active_texture(0x84C1)
        self.gl.glBindTexture(0x0DE1, self.lens_fbo.texture())
        program.setUniformValue("uLens", 1)
        self.draw(program, source, fw, fh)
        program.release(); self.vao.release()
        if measure:
            self.query.end(); self.gpu_pending = True
        if self.show_text and self.lyrics is not None:
            family = self.settings["lyric_font"]
            width = self.lyric_width()
            pan = lyric_pan(self.lyrics, self.lyric_time, width, family, self.lyric_end_time)
            key = (id(self.lyrics), self.scroll_position, pan, width, self.height(), self.devicePixelRatioF(), self.dark_text, family)
            if key != self.lyric_image_key:
                self.lyric_image = lyric_layer(self.lyrics, self.scroll_position, width, self.height(),
                                               self.devicePixelRatioF(), self.dark_text, family, pan, glass=True,
                                               target=getattr(self, "lyric_image", None))
                self.lyric_image_key = key
            painter = QPainter(self)
            if self.night:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            painter.drawImage(0, 0, self.lyric_image); painter.end()
        elif self.show_text:
            painter = QPainter(self)
            if self.night:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            font = lyric_font(self.settings["lyric_font"])
            painter.setFont(font)
            width = self.lyric_width()
            center = QRectF(16, self.height() / 2 - 28, width - 32, 36)
            title = painter.fontMetrics().elidedText(self.primary_text, Qt.TextElideMode.ElideRight,
                                                     max(1, width - 32))
            painter.setPen(QColor(0, 0, 0, 219) if self.dark_text else QColor(255, 255, 255, 240))
            painter.drawText(center, Qt.AlignmentFlag.AlignCenter, title)
            font.setPixelSize(13); painter.setFont(font)
            subtitle = painter.fontMetrics().elidedText(self.secondary_text, Qt.TextElideMode.ElideRight,
                                                        max(1, width - 32))
            painter.setPen(QColor(0, 0, 0, 153) if self.dark_text else QColor(255, 255, 255, 163))
            painter.drawText(QRectF(16, self.height() / 2 + 9, width - 32, 23),
                             Qt.AlignmentFlag.AlignCenter, subtitle)
            painter.end()

    def release_material_frame(self):
        """Drop hidden panel pixels while keeping its working GL composition intact."""
        if not self.ready:
            return
        self.makeCurrent()
        try:
            self.fbos = []; self.lens_fbo = None
            self.gl.glBindTexture(0x0DE1, self.texture.value)
            self.gl.glTexImage2D(0x0DE1,0,0x8058,1,1,0,0x80E1,0x1401,None)
            self.texture_size = self.lens_size = (0,0)
            self.frame_geometry = self.texture_rect = None
            self.scene_key = self.pixel_key = self.blur_key = self.uploaded_revision = None
            self.new_frame = False
        finally:
            self.doneCurrent()

    def shutdown(self):
        if not self.ready:
            return
        self.makeCurrent()
        self.query.destroy(); self.fbos = []; self.lens_fbo = None
        self.gl.glDeleteTextures(1, C.byref(self.texture))
        self.vbo.destroy(); self.vao.destroy()
        self.blur_program.removeAllShaders(); self.glass_program.removeAllShaders(); self.lens_program.removeAllShaders()
        self.ready = False
        self.doneCurrent()


class GlassWindow(QWidget):
    settings_requested = pyqtSignal()
    failed = pyqtSignal(str)
    capture_status_changed = pyqtSignal(bool)
    media_command = pyqtSignal(str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("MoonLyrics · 月光歌词")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window |
                            Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.capture = DesktopRegion()
        self.view = GlassView(settings, self.capture, self)
        self.view.failed.connect(self.fail)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.addWidget(self.view)
        self.timer = QTimer(self); self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.refresh)
        self.move_refresh = QTimer(self)
        self.move_refresh.setSingleShot(True)
        self.move_refresh.timeout.connect(lambda: self.refresh(moving=True))
        self.paused = False
        self.retry_delay = 0
        self.capture_needs_reset = False
        self.drag_anchor = None
        self.playback_active = False
        self.background_changed_at = time.monotonic()
        self.transport = QWidget(self)
        self.transport.setObjectName("lyricTransport")
        self.transport.installEventFilter(self)
        self.transport.setFixedWidth(136)
        self.transport.setCursor(Qt.CursorShape.ArrowCursor)
        self.transport.setStyleSheet("""
            QWidget#lyricTransport { background: transparent; border: none; }
            QToolButton { background: transparent; border: none; border-radius: 10px; }
            QToolButton:hover { background: rgba(220,237,255,35); }
            QToolButton:pressed { background: rgba(220,237,255,60); }
        """)
        # The full-frame renderer supplies the only glass surface and outer corner clip.
        self.transport_divider = QWidget(self.transport)
        self.transport_divider.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.transport_divider.setStyleSheet("background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 rgba(240,248,255,0), stop:0.5 rgba(240,248,255,60), stop:1 rgba(240,248,255,0));")
        buttons = QHBoxLayout(self.transport); buttons.setContentsMargins(10, 0, 10, 0); buttons.setSpacing(4)
        self.media_dark = False
        self.media_icons = {name: media_icon(name) for name in ("previous", "play", "pause", "next")}
        self.media_buttons = {}
        for action in ("previous", "toggle", "next"):
            button = QToolButton(self.transport)
            button.installEventFilter(self)
            button.setIcon(self.media_icons["play" if action == "toggle" else action])
            button.setIconSize(QSize(20, 20)); button.setFixedSize(36, 36)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, name=action: self.media_command.emit(name))
            self.media_buttons[action] = button
            buttons.addWidget(button)
        self.transport_effect = QGraphicsOpacityEffect(self.transport)
        self.transport_effect.setOpacity(0.)
        self.transport.setGraphicsEffect(self.transport_effect)
        self.transport.hide()
        self.transport_animation = QVariantAnimation(self)
        self.transport_animation.setDuration(180)
        self.transport_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.transport_animation.valueChanged.connect(self.animate_transport)
        self.transport_animation.finished.connect(self.transport_finished)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.resize(settings["width"], settings["height"])

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "move_refresh") and not self.paused and not self.retry_delay:
            self.move_refresh.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "transport"):
            self.transport.setGeometry(self.width() - self.transport.width(), 0, self.transport.width(), self.height())
            self.transport_divider.setGeometry(0, (self.height() - 44) // 2, 1, 44)

    def animate_transport(self, value):
        self.transport_effect.setOpacity(float(value))
        self.view.control_space = round(self.transport.width() * float(value))
        self.view.update()

    def transport_finished(self):
        if self.transport_effect.opacity() == 0:
            self.transport.hide()

    def reveal_transport(self, visible):
        visible = visible and self.drag_anchor is None
        self.transport_animation.stop()
        self.transport.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not visible)
        if visible:
            self.transport.show(); self.transport.raise_()
        self.transport_animation.setStartValue(self.transport_effect.opacity())
        self.transport_animation.setEndValue(1. if visible else 0.)
        self.transport_animation.start()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.reveal_transport(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.reveal_transport(False)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.protect)

    def protect(self):
        value = W.DWORD()
        hwnd = int(self.winId())
        if (not set_affinity(hwnd, 0) or not get_affinity(hwnd, C.byref(value))
                or value.value != 0):
            self.fail("无法启用可捕获的歌词窗口，请重新打开 MoonLyrics。")
            return
        self.ensure_topmost(force=True)
        if not self.paused:
            self.timer.start(self.retry_delay or self.refresh_interval())
        LOG.info("Lyric window is capturable; backdrop uses a local exclusion filter; HWND=%s", hwnd)

    def ensure_topmost(self, *, force=False):
        if not self.isVisible() or self.isMinimized():
            return False
        hwnd = int(self.winId())
        was_topmost = bool(get_window_long(hwnd, -20) & 0x8)
        ordinary_above = None
        if was_topmost and not force:
            # A surviving TOPMOST flag can disagree with the actual native Z-order.
            above = get_window(hwnd, 3)  # GW_HWNDPREV
            seen = set()
            for _ in range(1024):
                if not above or above in seen:
                    break
                seen.add(above)
                style = get_window_long(above, -16)
                if (style & 0x30000000 == 0x10000000  # visible, not minimized
                        and not get_window_long(above, -20) & 0x8):
                    cloaked = W.DWORD()
                    if window_attribute(above, 14, C.byref(cloaked), C.sizeof(cloaked)) >= 0 and not cloaked.value:
                        ordinary_above = above
                        break
                above = get_window(above, 3)
            if ordinary_above is None:
                return True
        # Reassert native Z-order without recreating the window or taking keyboard focus.
        flags = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER
        if not set_window_pos(hwnd, -1, 0, 0, 0, 0, flags):
            LOG.warning("Could not restore lyric topmost; HWND=%s error=%s", hwnd, C.get_last_error())
            return False
        LOG.info("Restored lyric topmost; HWND=%s previous_topmost=%s force=%s ordinary_above=%s",
                 hwnd, was_topmost, force, ordinary_above)
        return True

    def refresh_interval(self):
        recent = time.monotonic()-self.background_changed_at < .75
        limit = self.settings["fps"]
        rate = limit if recent or self.drag_anchor is not None else min(limit, 30 if self.playback_active else 15)
        return round(1000/rate)

    def update_refresh_schedule(self, *, wake=False):
        if wake:
            self.background_changed_at = time.monotonic()
        if self.paused or self.retry_delay or not self.isVisible():
            return
        interval = self.refresh_interval()
        if not self.timer.isActive() or self.timer.interval() != interval:
            self.timer.start(interval)

    def refresh(self, *, moving=False):
        if self.paused or self.isMinimized() or not self.isVisible():
            return
        try:
            if self.capture_needs_reset:
                self.capture.reset()
                self.capture_needs_reset = False
                self.timer.start(round(1000 / self.settings["fps"]))
            fresh = self.view.take_frame(refresh=False) if moving else self.view.take_frame()
            if self.view.background_changed:
                self.background_changed_at = time.monotonic()
            if not moving:
                self.update_refresh_schedule()
            if fresh is False:
                if moving:
                    self.view.update()
                return
        except OSError:
            self.view.new_frame = False
            self.capture_needs_reset = True
            if not self.retry_delay:
                LOG.exception("Desktop capture unavailable; scheduling automatic recovery")
                self.capture_status_changed.emit(True)
            self.retry_delay = min(5000, self.retry_delay * 2 or 1000)
            self.timer.start(self.retry_delay)
        except Exception as exc:
            LOG.exception("Desktop region capture failed")
            self.fail(str(exc))
        else:
            if self.retry_delay:
                self.retry_delay = 0
                LOG.info("Desktop capture recovered after rebuilding native resources")
                self.capture_status_changed.emit(False)
                self.update_refresh_schedule(wake=True)

    def fail(self, message):
        self.paused = True
        self.retry_delay = 0
        self.timer.stop()
        self.failed.emit(message)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
            self.settings_requested.emit()
            return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.settings_requested.emit()
            event.accept()
            return
        if self.transport.isVisible() and self.transport.geometry().contains(event.position().toPoint()):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_anchor = event.globalPosition().toPoint()
            self.drag_start = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.reveal_transport(False)
            self.refresh()

    def mouseMoveEvent(self, event):
        if self.drag_anchor is not None:
            desired = self.drag_start + event.globalPosition().toPoint() - self.drag_anchor
            screen = QApplication.screenAt(event.globalPosition().toPoint()) or self.screen()
            bounds = screen.availableGeometry()
            self.move(max(bounds.left(), min(desired.x(), bounds.right() - self.width() + 1)),
                      max(bounds.top(), min(desired.y(), bounds.bottom() - self.height() + 1)))

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        self.drag_anchor = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.reveal_transport(self.underMouse())
        self.refresh()

    def closeEvent(self, event):
        self.transport_animation.stop()
        self.timer.stop(); self.move_refresh.stop(); self.view.shutdown(); self.capture.close()
        super().closeEvent(event)


class Controls(SettingsWindow):
    def __init__(self, settings, save=True, media=True):
        super().__init__()
        self.settings, self.save_enabled = settings, save
        self.setWindowTitle("MoonLyrics · 月光歌词设置")
        self.glass = GlassWindow(settings)
        self.glass.failed.connect(self.failed)
        self.glass.capture_status_changed.connect(self.capture_status_changed)
        self.glass.media_command.connect(self.send_media_command)
        self.glass.view.tone_changed.connect(self.render_media_controls)
        self.media_command_busy = False
        self.error = None
        self.usage = Usage()
        self.samples = []
        self.sliders = {}
        self.playback = PlaybackSnapshot()
        self.lyric_clock = LyricClock()
        self.media_reader = None
        self.lyrics_worker = None
        self.lyric_request = 0
        self.lyric_key = None
        self.lyric_duration = 0.
        self.lyric_debounce = QTimer(self)
        self.lyric_debounce.setSingleShot(True)
        self.lyric_debounce.timeout.connect(self.request_lyrics)
        self.closing = False
        self.last_frames, self.last_sample_time = 0, time.perf_counter()
        self.lyrics_message = "播放歌曲后自动匹配同步歌词"
        self.runtime_message = "拖动后方的网页或播放视频，可检验真实背景取样。"
        build_settings(self, settings, media)
        self.sample_timer = QTimer(self)
        self.sample_timer.timeout.connect(lambda: self.sample(render=self.isVisible()))
        self.sample_timer.start(1000)
        screen = QApplication.primaryScreen().availableGeometry()
        self.glass.move(screen.center().x() - settings["width"] // 2, screen.top() + 160)
        self.resize(min(624, screen.width() - 60), min(992, screen.height() - 80))
        self.move(screen.left() + 40, max(screen.top() + 20, screen.bottom() - self.height() - 30))
        self.glass.settings_requested.connect(self.restore_settings)
        self.media_timer = QTimer(self); self.media_timer.timeout.connect(self.advance_display)
        self.media_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.next_settings_refresh = 0.
        self.attach_glass(GlassView, settings, self.glass.capture)
        self.glass.view.tone_changed.connect(self.sync_appearance)
        self.sync_appearance()
        self.prepare_translations()
        self.retranslate_ui()
        if media:
            self.lyrics_worker = LyricsWorker(self, cache_dir=APP_DIR / "cache" / "lyrics")
            self.lyrics_worker.result_ready.connect(self.accept_lyrics)
            self.lyrics_worker.cache_cleared.connect(self.cache_cleared)
            self.lyrics_worker.start()
            self.media_reader = AppleMusicReader(self)
            self.media_reader.snapshot_changed.connect(self.set_playback)
            self.media_reader.command_finished.connect(self.media_command_finished)
            self.media_reader.start()

    def ui_text(self, text, **values):
        return translate(self.settings["language"], text, **values)

    def prepare_translations(self):
        # Bind only static UI text; never translate song titles or replace live status with initial text.
        dynamic = (self.track_title, self.track_details, self.media_status, self.lyrics_status,
                   self.lyric_timing, self.stats, self.timing, self.hardware, self.status, self.pause, self.cache_status,
                   self.material_description,self.appearance_caption,self.reserve_source,self.position_label,self.runtime_summary)
        self.text_bindings = []
        objects = [self, self.glass] + self.findChildren(QWidget) + self.findChildren(QAction)
        for obj in objects:
            for getter, setter in (("text", "setText"), ("toolTip", "setToolTip"),
                                   ("accessibleName", "setAccessibleName"), ("windowTitle", "setWindowTitle")):
                if obj in dynamic and getter == "text":
                    continue
                read, write = getattr(obj, getter, None), getattr(obj, setter, None)
                if callable(read) and callable(write) and read() in TEXTS:
                    self.text_bindings.append((write, read()))

    def language_changed(self):
        self.settings["language"] = self.language.currentData()
        self.retranslate_ui()

    def font_changed(self, font):
        self.settings["lyric_font"] = font.family()
        self.glass.view.lyric_image_key = None
        self.glass.view.update()

    def appearance_changed(self):
        self.settings["appearance_mode"] = self.appearance.currentData()
        for mode, button in self.appearance_buttons.items():
            button.setChecked(mode == self.settings["appearance_mode"])
        self.glass.view.update_appearance()
        self.sync_appearance()

    def sync_appearance(self):
        if self.settings_backdrop:
            self.settings_backdrop.view.set_night(self.glass.view.night)
        if self.night != self.glass.view.night or not self.styleSheet():
            self.apply_appearance(self.glass.view.night)
        if hasattr(self,'appearance_caption'):
            mode='夜间' if self.night else '日间'
            text='系统当前为{mode}' if self.settings['appearance_mode']=='system' else '歌词条与设置面板使用{mode}外观'
            self.appearance_caption.setText(self.ui_text(text,mode=self.ui_text(mode)))
            self.update_ui_icons()

    def retranslate_ui(self):
        for setter, source in self.text_bindings:
            setter(self.ui_text(source))
        self.material_description.setText(self.ui_text(PRESETS["rich"]["description"]))
        for mode, button in self.appearance_buttons.items():
            button.setText(self.ui_text(SHORT_MODES[mode]))
        group=self.appearance_buttons['day'].parentWidget()
        group.setFixedWidth(max(252,sum(button.sizeHint().width() for button in self.appearance_buttons.values())+16))
        choices = [(self.storefront, dict(tr="土耳其", cn="中国大陆", hk="香港", tw="台湾", us="美国")),
                   (self.appearance, APPEARANCE_MODES)]
        choices += [(choice, LYRIC_SOURCES) for choice in self.source_choices]
        for combo, labels in choices:
            blocked = combo.blockSignals(True)
            for index in range(combo.count()):
                key = combo.itemData(index)
                combo.setItemText(index, self.ui_text(labels[key]))
            combo.blockSignals(blocked)
        for position, choice in enumerate(self.source_choices):
            choice.setAccessibleName(self.ui_text("第 {position} 优先歌词源", position=position + 1))
        self.version_label.setText(__version__)
        self.version_label.setToolTip(self.ui_text("版本：{version}",version=__version__))
        self.update_reserve_source()
        self.sync_appearance()
        self.cache_status.setText(self.ui_text(getattr(self, "cache_message", "成功匹配的歌词会自动缓存，下次播放直接读取。")))
        self.lyric_timing.setText(self.offset_label())
        self.set_lyrics_status(self.lyrics_message)
        document = self.glass.view.lyrics
        if document is not None:
            self.lyrics_status.setToolTip(f"{document.title} — {document.artist} · {self.ui_text(document.source)}")
        self.render_runtime_status()
        self.pause.setText(self.ui_text("重试刷新" if self.error else "继续刷新" if self.glass.paused else "暂停刷新"))
        self.update_track_labels()
        self.update_playback_display()
        self.render_stats()
        self.render_media_controls()

    def render_media_controls(self):
        if self.glass.media_dark != self.glass.view.dark_text:
            self.glass.media_dark = self.glass.view.dark_text
            self.glass.media_icons = {name: media_icon(name, self.glass.media_dark)
                                      for name in ("previous", "play", "pause", "next")}
        playing = self.playback.status == "playing"
        for action, button in self.glass.media_buttons.items():
            label = {"previous": "上一曲", "toggle": "暂停" if playing else "播放", "next": "下一曲"}[action]
            button.setAccessibleName(self.ui_text(label)); button.setToolTip(self.ui_text(label))
            button.setEnabled(bool(self.media_reader) and not self.closing and not self.media_command_busy
                              and action in self.playback.controls)
            button.setIcon(self.glass.media_icons[("pause" if playing else "play") if action == "toggle" else action])
            if hasattr(self, "settings_media_buttons"):
                other = self.settings_media_buttons[action]
                other.setAccessibleName(button.accessibleName()); other.setToolTip(button.toolTip())
                other.setEnabled(button.isEnabled())
                name=("pause" if playing else "play") if action=="toggle" else ("skip-back" if action=="previous" else "skip-forward")
                other.setIcon(settings_icon(name,'#1d1d1f' if self.glass.view.dark_text else '#f5f5f7'))

    def send_media_command(self, action):
        if self.closing or self.media_command_busy or action not in self.playback.controls:
            return
        if self.media_reader and self.media_reader.request_command(action):
            self.media_command_busy = True
            self.render_media_controls()
        else:
            self.media_command_finished(action, "播放控制暂不可用，请稍后重试")

    def media_command_finished(self, action, message):
        if self.closing:
            return
        self.media_command_busy = False
        self.runtime_message = message or "播放控制已发送"
        self.render_runtime_status()
        self.render_media_controls()
        if message and self.glass.isVisible():
            QToolTip.showText(self.glass.mapToGlobal(QPoint(self.glass.width() // 2, self.glass.height() + 8)),
                              self.ui_text(message), self.glass)

    def update_track_labels(self):
        self.track_title.setText(self.playback.title or self.ui_text("等待 Apple Music"))
        self.track_details.setText(" · ".join(value for value in (self.playback.artist, self.playback.album) if value)
                                   or self.ui_text("打开 Apple Music 并播放一首歌曲"))
        self.track_title.setToolTip(self.track_title.text());self.track_details.setToolTip(self.track_details.text())

    def set_lyrics_status(self, message):
        self.lyrics_message = message
        self.lyrics_status.setText(translate_message(self.settings["language"], message))
        self.lyrics_status.setToolTip(self.lyrics_status.text())

    def clear_cache(self):
        if self.lyrics_worker is None:
            return
        self.lyric_debounce.stop()
        self.lyric_request += 1
        self.clear_lyrics_cache.setEnabled(False)
        self.cache_message = "正在清空歌词缓存…"
        self.cache_status.setText(self.ui_text(self.cache_message))
        self.lyrics_worker.clear_cache()

    def cache_cleared(self, success):
        if self.closing:
            return
        self.clear_lyrics_cache.setEnabled(True)
        self.cache_message = "歌词缓存已清空" if success else "缓存暂时无法清空，请稍后重试"
        self.cache_status.setText(self.ui_text(self.cache_message))
        if self.glass.view.lyrics is None:
            self.set_lyrics_status("可点击重新匹配获取当前歌曲的歌词")

    def render_runtime_status(self):
        message = translate_message(self.settings["language"], self.error or self.runtime_message)
        self.status.setText(self.ui_text("已停止刷新：{message}", message=message) if self.error else message)

    def place_lyrics(self, position):
        screen = QApplication.screenAt(self.glass.frameGeometry().center()) or self.glass.screen()
        bounds = screen.availableGeometry()
        space_x, space_y = max(0, bounds.width() - self.glass.width()), max(0, bounds.height() - self.glass.height())
        margin_x, margin_y = min(24, space_x // 2), min(24, space_y // 2)
        x = (margin_x, space_x // 2, space_x - margin_x)[position % 3]
        y = (margin_y, space_y - margin_y)[position // 3]
        self.glass.move(bounds.left() + x, bounds.top() + y)
        self.glass.ensure_topmost(force=True)
        for slot,button in enumerate(self.position_buttons):button.setChecked(slot==position)
        self.position_label.setText(self.ui_text('屏幕{position}',position=self.ui_text(POSITION_LABELS[position])))
        self.update_ui_icons()

    def minimize_settings(self):
        if not self.closing:
            self.hide()

    def finish_minimize(self):
        # A queued minimize must not hide Settings after a restore request.
        if not self.closing and self.isMinimized():
            self.hide()

    def restore_settings(self):
        if not self.closing:
            self.showNormal(); self.raise_(); self.activateWindow()
            self.update_playback_display()
            self.render_stats()
            self.glass.ensure_topmost(force=True)

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == QEvent.Type.WindowStateChange and self.isMinimized()
                and hasattr(self, "glass")):
            QTimer.singleShot(0, self.finish_minimize)

    def set_playback(self, snapshot):
        if self.closing:
            return
        if snapshot.status == "playing" and self.playback.status != "playing":
            self.glass.ensure_topmost(force=True)
            if self.glass.retry_delay:
                self.glass.refresh()
        self.playback = snapshot
        was_active = self.glass.playback_active
        self.glass.playback_active = snapshot.status == "playing"
        self.glass.update_refresh_schedule(wake=self.glass.playback_active and not was_active)
        self.update_animation_schedule()
        self.render_media_controls()
        self.lyric_clock.update(snapshot)
        self.update_track_labels()
        self.media_status.setToolTip(snapshot.message)
        if (self.matching_key(snapshot) != self.lyric_key
                or snapshot.duration > 0 and abs(snapshot.duration - self.lyric_duration) > 1.):
            self.request_lyrics(defer=True)
        self.update_playback_display(update_settings=self.isVisible())

    def storefront_changed(self):
        self.settings["storefront"] = self.storefront.currentData()
        self.request_lyrics(force=True)

    def source_order_changed(self, position):
        order = list(self.settings["lyric_source_order"])
        selected = self.source_choices[position].currentData()
        if selected in order:
            previous = order.index(selected)
            order[previous], order[position] = order[position], selected
        else:
            order[position] = selected
        self.settings["lyric_source_order"] = tuple(order)
        for choice, source in zip(self.source_choices, order):
            blocked = choice.blockSignals(True)
            choice.setCurrentIndex(choice.findData(source))
            choice.blockSignals(blocked)
        self.update_reserve_source()
        self.request_lyrics(force=True)

    def update_reserve_source(self):
        reserved=[name for key,name in LYRIC_SOURCES.items() if key not in self.settings['lyric_source_order']]
        self.reserve_source.setText(self.ui_text('{source} 备用',source=' / '.join(self.ui_text(name) for name in reserved)))

    def matching_key(self, snapshot):
        text = lambda value: " ".join(value.split()).casefold()
        identity = (snapshot.source, text(snapshot.title), text(snapshot.artist), text(snapshot.album), self.settings["storefront"])
        duration = snapshot.duration
        if duration <= 0 and self.lyric_key and self.lyric_key[:5] == identity:
            duration = self.lyric_duration
        ready = bool(snapshot.title and snapshot.artist and duration > 0 and snapshot.status != "changing")
        return (*identity, ready)

    def request_lyrics(self, force=False, defer=False):
        if self.closing:
            return
        self.lyric_debounce.stop()
        snapshot = self.playback
        self.lyric_key = self.matching_key(snapshot)
        self.lyric_duration = snapshot.duration if snapshot.duration > 0 else (self.lyric_duration if self.lyric_key[-1] else 0.)
        self.lyric_request += 1
        self.glass.view.set_lyrics(None)
        valid = self.lyric_key[-1]
        self.retry_lyrics.setEnabled(valid and self.lyrics_worker is not None)
        self.lyrics_status.setToolTip("")
        self.set_lyrics_status(("等待歌曲信息稳定…" if defer else "正在匹配歌词…") if valid else "等待完整的歌曲信息")
        track = Track(snapshot.title, snapshot.artist, snapshot.album, self.lyric_duration) if valid else None
        if self.lyrics_worker is not None:
            self.lyrics_worker.request(self.lyric_request, None if defer else track, self.settings["storefront"], force,
                                       self.settings["lyric_source_order"])
        if defer and valid:
            self.lyric_debounce.start(600)

    def accept_lyrics(self, request_id, document, message):
        if self.closing or request_id != self.lyric_request:
            return
        self.set_lyrics_status(message)
        self.glass.view.set_lyrics(document, self.lyric_clock.position_at() + lyric_offset_seconds(self.settings))
        if document is not None:
            self.lyrics_status.setToolTip(f"{document.title} — {document.artist} · {self.ui_text(document.source)}")
        self.update_playback_display()

    def offset_label(self):
        offset = (self.settings["lyric_offset_ms"] - DEFAULTS["lyric_offset_ms"]) / 1000
        return self.ui_text("提前 {seconds:.1f} 秒" if offset > 0 else "延后 {seconds:.1f} 秒", seconds=abs(offset)) if offset else self.ui_text("标准")

    def change_lyric_offset(self, delta):
        lo, hi = LIMITS["lyric_offset_ms"]
        self.settings["lyric_offset_ms"] = max(lo, min(hi, self.settings["lyric_offset_ms"] + delta))
        self.lyric_timing.setText(self.offset_label())
        self.update_playback_display()

    def update_animation_schedule(self):
        if self.playback.status != "playing":
            self.media_timer.stop()
            return
        interval = max(33, round(1000/self.settings["fps"]))
        if not self.media_timer.isActive() or self.media_timer.interval() != interval:
            self.media_timer.start(interval)

    def advance_display(self):
        now = time.monotonic()
        update_settings = self.isVisible() and now >= self.next_settings_refresh
        if update_settings:
            self.next_settings_refresh = now+.1
        self.update_playback_display(update_settings=update_settings)

    def update_playback_display(self, *, update_settings=True):
        snapshot = self.playback
        offset = lyric_offset_seconds(self.settings)
        self.glass.view.set_lyric_time(self.lyric_clock.position_at() + offset,
                                      snapshot.duration + offset if snapshot.duration > 0 else None)
        if not update_settings and self.glass.view.lyrics is not None:
            return
        position = snapshot.position_at()
        state = self.ui_text(STATUS_LABELS.get(snapshot.status, "等待播放"))
        timing = format_time(position) + " / " + (format_time(snapshot.duration) if snapshot.duration else "--:--")
        if update_settings:
            self.media_status.setText(state); self.media_time.setText(timing)
            self.media_progress.setValue(round(position / snapshot.duration * 1000) if snapshot.duration else 0)
        primary = " — ".join(value for value in (snapshot.title, snapshot.artist) if value) or state
        secondary = timing + " · " + self.lyrics_status.text() if snapshot.title else self.ui_text("播放音乐后显示歌曲与进度")
        if (primary, secondary) != (self.glass.view.primary_text, self.glass.view.secondary_text):
            self.glass.view.primary_text, self.glass.view.secondary_text = primary, secondary
            self.glass.view.update()

    def fps_changed(self):
        self.settings["fps"] = self.fps.currentData()
        self.glass.update_refresh_schedule(wake=True)
        self.update_animation_schedule()

    def toggle_pause(self):
        self.glass.paused = not self.glass.paused
        if self.glass.paused:
            self.glass.timer.stop()
        else:
            if self.error:
                self.capture_status_changed(bool(self.glass.retry_delay))
            self.glass.protect()
        self.pause.setText(self.ui_text("重试刷新" if self.error else "继续刷新" if self.glass.paused else "暂停刷新"))

    def capture_status_changed(self, retrying):
        self.error = None
        self.runtime_message = "桌面取样暂时不可用，正在自动恢复…" if retrying else "桌面取样已恢复。"
        self.render_runtime_status()
        self.status.setObjectName("secondary")
        self.status.style().unpolish(self.status); self.status.style().polish(self.status)

    def failed(self, message):
        self.error = message
        self.render_runtime_status()
        self.status.setObjectName("errorStatus")
        self.status.style().unpolish(self.status); self.status.style().polish(self.status)
        self.pause.setText(self.ui_text("重试刷新"))

    def sample(self, *, render=True):
        self.glass.ensure_topmost()
        self.glass.view.update_appearance()
        self.sync_appearance()
        cpu, memory = self.usage.read()
        now = time.perf_counter(); view = self.glass.view
        fps = (view.frames - self.last_frames) / max(.01, now - self.last_sample_time)
        self.last_frames, self.last_sample_time = view.frames, now
        sample = dict(cpu=cpu, memory_mb=memory, fps=fps, capture_ms=view.capture_ms, gpu_ms=view.gpu_ms)
        self.samples.append(sample)
        self.samples = self.samples[-120:]
        if render:
            self.render_stats()

    def render_stats(self):
        view = self.glass.view
        if not self.samples:
            self.stats.setText(self.ui_text("正在测量 CPU、内存与实际帧率…"))
            self.hardware.setText(self.ui_text("正在识别显卡…"))
            return
        sample = self.samples[-1]
        self.runtime_summary.setText('CPU {cpu:.1f}% · {memory_mb:.0f} MB · {fps:.0f} FPS'.format(**sample))
        self.stats.setText(self.ui_text("CPU  {cpu:.2f}%     内存  {memory_mb:.1f} MB     实际帧率  {fps:.1f} FPS", **sample))
        gpu = "—" if view.gpu_ms is None else f"{view.gpu_ms:.2f} ms"
        self.timing.setText(self.ui_text("桌面取样  {capture_ms:.2f} ms / 帧     GPU 材质绘制  {gpu}", capture_ms=view.capture_ms, gpu=gpu))
        self.hardware.setText(self.ui_text("显卡：{renderer}", renderer=self.ui_text(view.renderer)))

    def closeEvent(self, event):
        self.closing = True
        self.render_media_controls()
        self.lyric_debounce.stop()
        readers = [reader for reader in (self.media_reader, self.lyrics_worker) if reader is not None]
        for reader in readers:
            reader.stop()
        if any(not reader.wait(100) for reader in readers):
            self.set_lyrics_status("正在结束后台请求…")
            event.ignore(); QTimer.singleShot(100, self.close)
            return
        self.media_timer.stop()
        self.sample_timer.stop()
        self.glass.close()
        if self.save_enabled:
            try:
                APP_DIR.mkdir(parents=True, exist_ok=True)
                (APP_DIR / "settings.json").write_text(json.dumps(self.settings), encoding="utf-8")
            except OSError:
                LOG.exception("Settings save failed")
        super().closeEvent(event)
        QApplication.instance().quit()


def self_test(app, control, report_path, preview_path):
    from dataclasses import replace
    from unittest.mock import Mock, patch
    from PyQt6.QtTest import QTest
    from PyQt6.QtGui import QFontDatabase, QFontInfo, QImage
    from PIL import Image, ImageChops, ImageGrab, ImageStat
    from .windows_capture import client_origin

    class Calibration(QWidget):
        pattern = "checker"
        period = 4

        def paintEvent(self, event):
            p = QPainter(self)
            if self.pattern == "checker":
                for y in range(0, self.height(), 12):
                    for x in range(0, self.width(), 12):
                        p.fillRect(x, y, 12, 12, QColor("#297ebb" if (x // 12 + y // 12) % 2 else "#f2c070"))
            else:
                p.fillRect(self.rect(), QColor("#202020"))
                if self.pattern == "vertical":
                    for x in range(0, self.width(), self.period):
                        p.fillRect(x, 0, self.period // 2, self.height(), QColor("#e0e0e0"))
                else:
                    for y in range(0, self.height(), self.period):
                        p.fillRect(0, y, self.width(), self.period // 2, QColor("#e0e0e0"))
            p.end()

    def image():
        q = control.glass.view.grabFramebuffer().convertToFormat(QImage.Format.Format_RGBA8888)
        return Image.frombytes("RGBA", (q.width(), q.height()), q.bits().asstring(q.sizeInBytes())).convert("RGB")

    def sampled_frame(pad):
        deadline = time.perf_counter() + 3
        while glass.capture.capture(int(glass.winId()), pad) is None:
            assert time.perf_counter() < deadline, "Timed out waiting for a fresh background frame"
            QTest.qWait(10)

    def parameters(**values):
        for key, number in values.items():
            if key == "fps": control.fps.setCurrentIndex(control.fps.findData(number))
            elif key == "appearance_mode": control.appearance.setCurrentIndex(control.appearance.findData(number))
            else: control.sliders[key].setValue(number // (10 if key == "width" else 1))
        control.glass.refresh(); QTest.qWait(300)
        assert control.error is None, control.error

    result = {}
    calibration = Calibration()
    calibration.setWindowTitle("MoonLyrics render calibration")
    calibration.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    screen = QApplication.primaryScreen().availableGeometry()
    calibration.setGeometry(screen.left() + 100, screen.top() + 70, 1000, 440)
    calibration.show()
    glass = control.glass
    glass.move(calibration.x() + 110, calibration.y() + 150)
    glass.show(); QTest.qWait(700)
    try:
        font_id = load_bundled_font()
        assert font_id >= 0, "The packaged default font is missing or invalid"
        QFontDatabase.removeApplicationFont(font_id)
        default_font = QFontInfo(lyric_font(None))
        assert default_font.family() == "Noto Sans SC Black" and default_font.weight() == QFont.Weight.Black
        assert "SIL OPEN FONT LICENSE" in (Path(__file__).resolve().parents[1] / "assets" / "NotoSansSC-OFL.txt").read_text(encoding="utf-8")
        result["bundled_default_font"] = dict(family=default_font.family(), weight=default_font.weight(), license_included=True)
        assert [control.appearance.itemData(i) for i in range(control.appearance.count())] == list(APPEARANCE_MODES)
        assert set(control.sliders) == {"width", "height"}
        migrated = normalized(dict(transparency_level=47, transparency_scale=100, width=640, lyric_font="Segoe UI"))
        assert migrated["appearance_mode"] == "system" and migrated["width"] == 640 and migrated["lyric_font"] == "Segoe UI"
        assert "transparency_level" not in migrated
        for mode in APPEARANCE_MODES:
            restored = normalized(dict(appearance_mode=mode))
            assert restored["appearance_mode"] == mode and normalized(json.loads(json.dumps(restored))) == restored
        assert normalized(dict(appearance_mode=[]))["appearance_mode"] == "system"
        assert "MIT License" in (Path(__file__).resolve().parents[1] / "assets" / "Glasswork-LICENSE.txt").read_text(encoding="utf-8")
        assert "MIT License" in (Path(__file__).resolve().parents[1] / "assets" / "Atrium-LICENSE.txt").read_text(encoding="utf-8")
        result["appearance_modes"] = dict(choices=list(APPEARANCE_MODES), default="system", legacy_settings_migrated=True,
                                          glasswork_license_included=True, atrium_license_included=True)
        assert glass.view.ready, "GPU renderer did not initialize"
        assert not control.error, control.error
        glass.view.show_text = False
        parameters(appearance_mode="day")
        glass.timer.stop(); glass.hide(); QTest.qWait(120)
        geometry = glass.view.frame_geometry
        sampled_frame(geometry[2])
        baseline = C.string_at(glass.capture.bits, math.prod(glass.capture.size) * 4)
        raw_image = Image.frombytes("RGB", glass.capture.size, baseline, "raw", "BGRX", 0,
                                    1 if glass.capture.top_down else -1)
        colors = sorted(raw_image.getcolors(raw_image.width * raw_image.height), reverse=True)
        result["calibration_dominant_colors"] = colors[:2]
        assert all(color in [(41, 126, 187), (242, 192, 112)] for _, color in colors[:2]), "Calibration window is obscured"
        glass.show(); QTest.qWait(200); glass.timer.stop()
        sampled_frame(geometry[2])
        after = C.string_at(glass.capture.bits, len(baseline))
        difference = sum(abs(a - b) for a, b in zip(baseline, after)) / len(baseline)
        assert difference < .05, f"Capture includes overlay or changing background: {difference}"
        result["capture_exclusion_mean_byte_difference"] = difference
        origin = W.POINT()
        assert client_origin(int(glass.winId()), C.byref(origin))
        fw, fh, pad = geometry
        visible = ImageGrab.grab(bbox=(origin.x, origin.y, origin.x + fw, origin.y + fh), all_screens=True)
        clean_background = raw_image.crop((pad, pad, pad + fw, pad + fh))
        visible_difference = sum(ImageStat.Stat(ImageChops.difference(visible.convert("RGB"), clean_background)).mean) / 3
        assert visible_difference > 5, "The lyric window is still excluded from ordinary screenshots"
        assert not get_window_long(int(glass.winId()), -20) & 0x80, "The lyric window is a tool window"
        owner = api(user32, "GetWindow", W.HWND, W.HWND, W.UINT)(int(glass.winId()), 4)
        assert not owner, "The lyric window is owned by another window"
        result["capture_compatibility"] = dict(ordinary_screenshot_visible=True, private_sampling_excludes_overlay=True,
                                               normal_unowned_window=True, screenshot_mean_difference=visible_difference)
        frame_count = glass.view.frames

        with patch.object(glass.view, "take_frame", side_effect=C.WinError(5)):
            glass.refresh()
        assert glass.retry_delay == 1000 and glass.timer.isActive() and not glass.paused
        # Repaint the retained GPU texture while the background sampler cannot initialize.
        with patch("moonlyrics.windows_capture.mag_initialize", return_value=False):
            QTest.qWait(1200)
            assert glass.retry_delay == 2000 and glass.capture.size == (0, 0)
            image()
            assert glass.view.ready and control.error is None
        calibration.pattern = "vertical"; calibration.update()
        deadline = time.perf_counter() + 3
        while glass.retry_delay and time.perf_counter() < deadline:
            QTest.qWait(50)
        assert glass.retry_delay == 0 and glass.timer.isActive() and not glass.paused
        assert glass.view.frames > frame_count and glass.view.ready and control.error is None
        assert "已恢复" in control.status.text() and control.status.objectName() != "errorStatus"
        fresh = C.string_at(glass.capture.bits, len(baseline))
        assert sum(abs(a - b) for a, b in zip(baseline, fresh)) / len(baseline) > 5, "Recovered capture still contains the old background"
        affinity = W.DWORD()
        assert get_affinity(int(glass.winId()), C.byref(affinity)) and affinity.value == 0
        result["capture_recovery"] = dict(access_denied=True, automatic_retry=True, native_resources_rebuilt=True,
                                            retained_texture=True, fresh_background=True, exclusion_preserved=True)
        glass.timer.stop()
        calibration.pattern = "checker"; calibration.update(); QTest.qWait(100)
        parameters(appearance_mode="day"); day = image()
        parameters(appearance_mode="night"); night = image()
        center = (round(day.width*.2), round(day.height*.35), round(day.width*.8), round(day.height*.65))
        difference = sum(ImageStat.Stat(ImageChops.difference(day, night).crop(center)).mean)/3
        assert difference > 15, difference
        assert glass.view.blur_key == 2*glass.view.devicePixelRatioF()
        assert glass.view.lens_fbo.isValid() and glass.view.lens_size == (glass.width(), glass.height())
        result["glasswork_material"] = dict(day_night_difference=difference, blur_css_px=2, cached_rim_lens=True)
        # Changing appearance must repaint retained pixels without resuming paused capture.
        control.toggle_pause(); assert glass.paused and not glass.timer.isActive()
        before = glass.view.frames
        control.appearance.setCurrentIndex(control.appearance.findData("day")); QTest.qWait(100)
        assert glass.view.dark_text and glass.view.frames > before and glass.paused and not glass.timer.isActive()
        with patch("moonlyrics.windows.system_is_dark", return_value=True):
            control.appearance.setCurrentIndex(control.appearance.findData("system")); control.sample()
            assert glass.view.night
            control.appearance.setCurrentIndex(control.appearance.findData("day")); control.sample()
            assert not glass.view.night
        with patch("moonlyrics.windows.system_is_dark", return_value=False):
            control.appearance.setCurrentIndex(control.appearance.findData("system")); control.sample()
            assert not glass.view.night
            control.appearance.setCurrentIndex(control.appearance.findData("night")); control.sample()
            assert glass.view.night
        result["appearance_modes"].update(paused_refresh_switch=True, system_theme_follow=True, explicit_override=True)
        control.toggle_pause(); assert not glass.paused
        parameters(appearance_mode="day")
        clear_frame = glass.view.grabFramebuffer()
        assert clear_frame.pixelColor(clear_frame.width()//2, clear_frame.height()//2).alpha() == 255
        result["background_alpha"] = 255
        glass.view.show_text = True
        result["profiles"] = {}
        for name, values in (("day_30fps", dict(appearance_mode="day", fps=30)),
                             ("night_30fps", dict(appearance_mode="night", fps=30))):
            parameters(**values); glass.timer.start(round(1000 / values["fps"]))
            QTest.qWait(1200); control.samples.clear()
            for _ in range(7): QTest.qWait(1000)
            samples = list(control.samples)
            result["profiles"][name] = {key: statistics.mean(s[key] for s in samples if s[key] is not None)
                                        for key in samples[0] if any(s[key] is not None for s in samples)}
        parameters(appearance_mode="day", width=580, height=80, fps=30)
        location = glass.pos()
        QTest.mousePress(glass, Qt.MouseButton.LeftButton, pos=QPoint(80, 40))
        glass.timer.stop(); QTest.qWait(80)
        origin_fn = api(user32, "ClientToScreen", W.BOOL, W.HWND, C.POINTER(W.POINT))
        tracked, before_frames = 0, glass.view.frames
        held_dark_text = glass.view.dark_text
        with patch.object(glass.view.gl, "glTexSubImage2D", wraps=glass.view.gl.glTexSubImage2D) as upload, \
                patch.object(glass.view.gl, "glTexImage2D", wraps=glass.view.gl.glTexImage2D) as allocate, \
                patch.object(glass.view, "draw", wraps=glass.view.draw) as draw:
            for step in range(30):
                glass.move(location + QPoint(step*4, 0)); QTest.qWait(12)
                origin = W.POINT(); assert origin_fn(int(glass.winId()), C.byref(origin))
                pad = glass.view.frame_geometry[2]
                key = glass.capture.frame_key
                tracked += bool(key and key[1:3] == (origin.x-pad, origin.y-pad))
        gpu_uploads = upload.call_count+allocate.call_count
        blur_passes = sum(call.args[0] is glass.view.blur_program for call in draw.call_args_list)
        assert tracked >= 20 and glass.view.frames-before_frames >= 10, (tracked, glass.view.frames-before_frames)
        assert gpu_uploads <= 3 and blur_passes <= 6, (gpu_uploads, blur_passes)
        assert glass.view.dark_text == held_dark_text
        result["drag_background_tracking"] = dict(current_position_crops=tracked, steps=30,
                                                   rendered_frames=glass.view.frames-before_frames,
                                                   gpu_uploads=gpu_uploads, blur_passes=blur_passes, stable_appearance=True)
        QTest.mouseRelease(glass, Qt.MouseButton.LeftButton, pos=QPoint(80, 40))
        glass.move(location); QTest.qWait(80)
        control.set_playback(PlaybackSnapshot(title="月光轻轻落在窗台", artist="MoonLyrics",
                                             album="播放信息测试", status="paused", position=64., duration=270.))
        control.update_playback_display()  # Hidden settings refresh only when explicitly requested.
        assert control.media_time.text() == "1:04 / 4:30"
        assert control.media_status.text() == "已暂停"
        assert control.media_progress.value() == round(64 / 270 * 1000)
        assert "月光轻轻落在窗台" in glass.view.primary_text
        result["playback_display"] = True
        original_order = control.settings["lyric_source_order"]
        control.source_choices[0].setCurrentIndex(control.source_choices[0].findData("qq"))
        assert control.settings["lyric_source_order"][0] == "qq"
        assert len(set(choice.currentData() for choice in control.source_choices)) == 3
        assert all(choice.count() == len(LYRIC_SOURCES) for choice in control.source_choices)
        assert normalized(json.loads(json.dumps(control.settings)))["lyric_source_order"] == control.settings["lyric_source_order"]
        for choice, source in zip(control.source_choices, original_order):
            choice.setCurrentIndex(choice.findData(source))
        assert control.settings["lyric_source_order"] == original_order
        result["source_priority_controls"] = True
        assert normalize("遠走高飛") == normalize("远走高飞")
        control.media_timer.stop()
        control.lyric_debounce.stop()
        control.change_lyric_offset(DEFAULTS["lyric_offset_ms"] - control.settings["lyric_offset_ms"])
        assert control.lyric_timing.text() == "标准"
        document = Lyrics((LyricLine(60., "上面一行轻轻隐去"), LyricLine(64., "中间一行清晰显示"),
                           LyricLine(68., "下面一行渐渐出现"), LyricLine(72., "下一句等待时光")), "LRCLIB")
        control.lyric_request = 8
        control.accept_lyrics(7, document, "旧请求")
        assert glass.view.lyrics is None, "An old song replaced the current lyrics"
        control.accept_lyrics(8, document, "已匹配 · LRCLIB · 4 行")
        assert glass.view.lyric_index == 1
        glass.view.set_lyric_time(67.675)
        assert abs(glass.view.scroll_position - 1.5) < 1e-7, "Lyric movement did not ease in before the cue"
        glass.view.set_lyric_time(68.)
        assert glass.view.scroll_position == 2.
        base = time.perf_counter()
        scroll_samples = []
        def playback_at(position, elapsed, status="playing"):
            with patch("time.perf_counter", return_value=base + elapsed):
                control.set_playback(PlaybackSnapshot(title="月光轻轻落在窗台", artist="MoonLyrics",
                                                     album="播放信息测试", status=status, position=position - 1,
                                                     duration=270., observed_at=base + elapsed))
                scroll_samples.append(glass.view.scroll_position)
        for elapsed, position in ((0., 67.85), (.2, 68.05), (.25, 67.96), (.3, 68.03), (.35, 67.98), (.5, 68.12)):
            playback_at(position, elapsed)
        assert all(b >= a for a, b in zip(scroll_samples, scroll_samples[1:])), scroll_samples
        playback_at(72.1, .6)
        assert glass.view.scroll_position == 3., "Forward seek did not reset the visible lyric"
        playback_at(60.1, .7)
        assert glass.view.scroll_position == 0., "Backward seek did not reset the visible lyric"
        playback_at(67.9, .8, "paused")
        assert glass.view.lyric_index == 1
        control.change_lyric_offset(100)
        assert glass.view.lyric_index == 2 and control.lyric_timing.text() == "提前 0.1 秒"
        control.change_lyric_offset(-100)
        assert glass.view.lyric_index == 1
        control.change_lyric_offset(-1000)
        assert control.lyric_timing.text() == "延后 1.0 秒"
        next(button for button in control.findChildren(QPushButton) if button.text() == "重置").click()
        assert control.settings["lyric_offset_ms"] == 1000 and control.lyric_timing.text() == "标准"
        result["calibration_base_advance_seconds"] = lyric_offset_seconds(control.settings)
        playback_at(64.1, .9, "paused")
        QTest.qWait(100)
        control.update_playback_display()
        assert glass.view.scroll_position == 1.
        layer = lyric_layer(document, 1., glass.width(), glass.height(), glass.devicePixelRatioF(), False)
        rgba = layer.convertToFormat(QImage.Format.Format_RGBA8888)
        alpha = Image.frombytes("RGBA", (rgba.width(), rgba.height()), rgba.bits().asstring(rgba.sizeInBytes())).getchannel("A")
        assert alpha.getextrema()[1] > 200
        assert max(alpha.crop((0, 0, alpha.width, 1)).getdata()) <= 4
        assert max(alpha.crop((0, alpha.height - 1, alpha.width, alpha.height)).getdata()) <= 4
        assert alpha.getpixel((0, 0)) == alpha.getpixel((alpha.width - 1, 0)) == 0
        areas = [ImageStat.Stat(alpha.crop((0, round(alpha.height * a), alpha.width, round(alpha.height * b)))).mean[0]
                 for a, b in ((0., .28), (.34, .66), (.72, 1.))]
        assert areas[1] > max(areas[0], areas[2]) * 1.5, areas
        result["lyrics_viewport"] = dict(clipped=True, edge_fade=True, center_emphasis=True,
                                         animated_scroll=True, seek_and_pause=True, stale_result_rejected=True,
                                         jitter_no_rewind=True, arrives_on_cue=True, timing_calibration=True)
        original_font = control.settings["lyric_font"]
        font_document = Lyrics((LyricLine(60., "Previous line"), LyricLine(64., "Moonlight · 月光"),
                                LyricLine(74., "Next line")))
        glass.view.set_lyrics(font_document, 64.1); QTest.qWait(50)
        before_font = image()
        font_index = control.font_choice.findText("Consolas")
        assert font_index >= 0, "System font selector did not load the installed fonts"
        control.font_choice.setCurrentIndex(font_index); QTest.qWait(50)
        assert control.settings["lyric_font"] == "Consolas"
        assert sum(ImageStat.Stat(ImageChops.difference(before_font, image())).mean) > .2
        assert normalized(json.loads(json.dumps(control.settings)))["lyric_font"] == "Consolas"
        long_document = Lyrics((LyricLine(64., "Moonlight through the window · 月光落在窗台 · " * 3),
                                LyricLine(74., "Next line")))
        glass.view.set_lyrics(long_document, 64.1); QTest.qWait(50)
        before_pan = image()
        glass.view.set_lyric_time(73.25); QTest.qWait(50)
        assert glass.view.pan_position == (0, 1.) and glass.view.scroll_position == 0., (glass.view.pan_position, glass.view.scroll_position)
        assert sum(ImageStat.Stat(ImageChops.difference(before_pan, image())).mean) > .2
        glass.view.set_lyric_time(64.); assert glass.view.pan_position == (0, 0.)
        control.font_choice.setCurrentFont(QFont(original_font))
        glass.view.set_lyrics(document, 64.1)
        result["lyric_typography"] = dict(live_font_switch=True, font_saved=True, fixed_font_size=True,
                                          long_line_pan=True, seek_resets_pan=True)
        # Desktop sampling was verified above; keep it out of settings animation timing checks.
        glass.timer.stop()
        assert not app.windowIcon().isNull() and not control.windowIcon().isNull()
        control.move(screen.right() - control.width() - 30, screen.top() + 30)
        control.show(); QTest.qWait(200)
        original_size = control.size()
        control.resize(control.minimumWidth(), control.minimumHeight()); QTest.qWait(100)
        assert control.scroll.widget().width() <= control.scroll.viewport().width(), "Settings fields are clipped at minimum width"
        control.resize(original_size)
        QTest.mouseDClick(control.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(250, 25))
        QTest.qWait(100); assert control.isMaximized()
        QTest.mouseDClick(control.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(250, 25))
        QTest.qWait(100); assert not control.isMaximized()
        settings_backdrop = control.settings_backdrop
        deadline = time.perf_counter()+3
        while not control.backdrop_active and time.perf_counter()<deadline:
            QTest.qWait(50)
        assert control.backdrop_active and settings_backdrop.view.ready, "Shared settings glass did not initialize"
        # Check the actual screen before any widget/FBO grab. GPU readback
        # alone can pass even when the native window omits the glass layer.
        calibration.hide()
        screen_backdrop=QWidget()
        screen_backdrop.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        screen_backdrop.setGeometry(control.geometry())
        screen_backdrop.setStyleSheet('background:#297ebb')
        screen_backdrop.show();control.raise_();control.activateWindow()
        try:
            for mode in ('day','night'):
                control.appearance_buttons[mode].click();QTest.qWait(150)
                origin=W.POINT();assert client_origin(int(control.winId()),C.byref(origin))
                scale=control.devicePixelRatioF();px=origin.x+round(12*scale);py=origin.y+round(200*scale)
                color=ImageGrab.grab(bbox=(px,py,px+1,py+1),all_screens=True).getpixel((0,0))[:3]
                assert sum(abs(a-b) for a,b in zip(color,(41,126,187)))>65, ('Settings screen backdrop missing',mode,color)
            control.hide();QTest.qWait(3250)
            assert settings_backdrop.released and settings_backdrop.view.texture_size==(0,0)
            control.restore_settings()
            deadline=time.perf_counter()+3
            while not control.backdrop_active and time.perf_counter()<deadline:QTest.qWait(50)
            QTest.qWait(100)
            assert control.backdrop_active and settings_backdrop.view.ready
            origin=W.POINT();assert client_origin(int(control.winId()),C.byref(origin))
            px=origin.x+round(12*scale);py=origin.y+round(200*scale)
            color=ImageGrab.grab(bbox=(px,py,px+1,py+1),all_screens=True).getpixel((0,0))[:3]
            assert sum(abs(a-b) for a,b in zip(color,(41,126,187)))>65, ('Released settings backdrop did not recover',color)
        finally:
            screen_backdrop.close();screen_backdrop.deleteLater();calibration.show()
        settings_frame = settings_backdrop.view.grabFramebuffer()
        assert settings_frame.pixelColor(settings_frame.width()//2, settings_frame.height()//2).alpha() == 255
        assert settings_frame.pixelColor(2, 2).alpha() == 0, "Settings corners expose a second background surface"
        for mode in APPEARANCE_MODES:
            control.appearance_buttons[mode].click(); QTest.qWait(100)
            assert control.settings["appearance_mode"] == mode and control.windowOpacity() == 1
            assert control.night == glass.view.night == settings_backdrop.view.night
            assert control.appearance_buttons[mode].isChecked()
            if preview_path:
                control.grab().save(str(preview_path.with_name(f"settings-window-{mode}.png")))
        control.appearance.setCurrentIndex(control.appearance.findData("day"))
        control.minimize_settings(); QTest.qWait(50)
        visible = api(user32, "IsWindowVisible", W.BOOL, W.HWND)
        assert not control.isVisible() and not visible(int(control.winId())) and glass.isVisible()
        assert not settings_backdrop.timer.isActive() and settings_backdrop.capture.size == (0, 0)
        assert not hasattr(control, "tray") and not hasattr(control, "lock")
        style = get_window_long(int(glass.winId()), -20)
        assert style & 0x08000000 and not style & 0x00040000, "Lyric bar creates a taskbar entry"
        QTest.mouseClick(glass, Qt.MouseButton.RightButton, pos=QPoint(80, 40)); QTest.qWait(50)
        assert control.isVisible() and not control.isMinimized()
        control.showMinimized(); QTest.qWait(50)
        assert not control.isVisible() and glass.isVisible()
        QTest.mouseClick(glass, Qt.MouseButton.RightButton, pos=QPoint(80, 40)); QTest.qWait(50)
        assert control.isVisible() and not control.isMinimized()
        control.showMinimized(); control.restore_settings(); QTest.qWait(50)
        assert control.isVisible(), "A stale minimize hid restored Settings"
        control.restore_settings(); QTest.qWait(100)
        settings_backdrop.stop(); settings_backdrop.view.hide()
        control.backdrop_active = False; control.update(); QTest.qWait(50)
        fallback = control.grab().toImage(); ratio = fallback.devicePixelRatio()
        pixel = fallback.pixelColor(round(15*ratio), round(200*ratio))
        assert pixel.alpha() == 255 and pixel.getRgb()[:3] == control.material[:3], pixel.getRgb()
        settings_backdrop.view.show(); settings_backdrop.start(); QTest.qWait(150)
        scroll = control.scroll.verticalScrollBar()
        scroll.setValue(scroll.maximum()); QTest.qWait(100)
        assert scroll.value() == scroll.maximum()
        assert control.runtime_button.isVisibleTo(control.scroll)
        control.runtime_button.click(); QTest.qWait(50)
        assert control.runtime_menu.isVisible() and control.status.isVisibleTo(control.runtime_menu)
        assert control.language.isVisibleTo(control.runtime_menu) and control.clear_lyrics_cache.isVisibleTo(control.runtime_menu)
        control.runtime_menu.hide()
        scroll.setValue(0)
        result["settings_ui"] = dict(appearance_modes=list(APPEARANCE_MODES), icon=True, scroll=True, minimum_size=True, title_bar=True, opaque_fallback=True,
                                      shared_glass=control.backdrop_active, shared_day_night=True,
                                      hidden_sampling_stops=True, hidden_minimize=True,
                                      approved_mockup_layout=True, advanced_controls_in_status=True,
                                      real_screen_background=True,released_background_recovers=True)
        control.hide()
        QTest.mouseClick(glass, Qt.MouseButton.RightButton, pos=QPoint(80, 40)); QTest.qWait(50)
        assert control.isVisible() and not control.isMinimized() and glass.drag_anchor is None
        assert not glass.windowFlags() & Qt.WindowType.WindowTransparentForInput
        result["settings_recovery"] = dict(hidden_minimize=True, lyric_right_click=True,
            no_tray=True, no_hotkey=True, no_lock=True, stale_minimize_guard=True)
        for language in LANGUAGES:
            control.language.setCurrentIndex(control.language.findData(language)); QTest.qWait(100)
            assert control.scroll.widget().width() <= control.scroll.viewport().width(), f"Settings overflow in {language}"
            assert control.retry_lyrics.text() == translate(language, "重新匹配")
            assert all(control.appearance.itemText(i) == translate(language, label)
                       for i, label in enumerate(APPEARANCE_MODES.values()))
            assert control.lyric_timing.text() == translate(language, "标准")
            assert __version__ in control.version_label.text()
            if preview_path:
                control.scroll.widget().grab().save(str(preview_path.with_name(f"settings-{language}.png")))
        control.language.setCurrentIndex(control.language.findData("zh_CN"))
        control.fps.setCurrentIndex(control.fps.findData(120))
        assert control.settings["fps"] == 120 and glass.timer.interval() == round(1000 / 120)
        QTest.qWait(200)
        control.fps.setCurrentIndex(control.fps.findData(30)); glass.timer.stop()
        assert control.version_label.isVisibleTo(control.scroll)
        location = glass.pos()
        bounds = glass.screen().availableGeometry()
        for button in control.position_buttons:
            button.click()
            assert bounds.contains(glass.geometry()), "A quick position put the lyric bar outside the work area"
        glass.move(location)
        result["settings_controls"] = dict(languages=list(LANGUAGES), width_step_px=10, max_fps=120,
                                           calibration_standard=True, quick_positions=6, version=__version__)
        original_reader, original_playback = control.media_reader, control.playback
        fake_reader = Mock(request_command=Mock(return_value=True))
        try:
            control.media_reader = fake_reader
            control.set_playback(replace(original_playback, controls=("previous", "toggle", "next")))
            sampled_frame(glass.view.frame_geometry[2])
            background = C.string_at(glass.capture.bits, math.prod(glass.capture.size) * 4)
            glass.reveal_transport(True); QTest.qWait(230)
            assert glass.transport.isVisible() and glass.view.control_space == glass.transport.width()
            assert glass.view.lyric_width() == glass.transport.x()
            assert glass.transport.geometry().right() == glass.rect().right()
            assert glass.transport.y() == 0 and glass.transport.height() == glass.height()
            sampled_frame(glass.view.frame_geometry[2])
            after_controls = C.string_at(glass.capture.bits, len(background))
            assert sum(abs(a - b) for a, b in zip(background, after_controls)) / len(background) < .05
            location = glass.pos()
            for action, button in glass.media_buttons.items():
                assert button.isEnabled() and not button.icon().isNull()
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                fake_reader.request_command.assert_called_with(action)
                assert control.media_command_busy and not button.isEnabled()
                assert glass.pos() == location and glass.drag_anchor is None
                control.media_command_finished(action, "")
            assert fake_reader.request_command.call_count == 3
            if preview_path:
                origin = W.POINT(); assert client_origin(int(glass.winId()), C.byref(origin))
                rect = W.RECT(); assert api(user32, "GetClientRect", W.BOOL, W.HWND, C.POINTER(W.RECT))(int(glass.winId()), C.byref(rect))
                ImageGrab.grab(bbox=(origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom), all_screens=True).save(str(preview_path.with_name("hover-controls.png")))
            glass.reveal_transport(False); QTest.qWait(210)
            assert not glass.transport.isVisible() and glass.view.control_space == 0
        finally:
            control.media_reader = original_reader
            control.set_playback(original_playback)
        result["media_controls"] = dict(hover_fade=True, command_routing=True,
                                        no_drag=True, private_capture_exclusion=True, shared_glass=True, aligned_outer_corners=True)
        glass.view.update(); QTest.qWait(100)
        result["renderer"] = glass.view.renderer
        result["device_pixel_ratio"] = glass.devicePixelRatioF()
        result["ok"] = True
        if preview_path:
            glass.view.grabFramebuffer().save(str(preview_path))
            control.grab().save(str(preview_path.with_name("glass-controls.png")))
            for mode in APPEARANCE_MODES:
                parameters(appearance_mode=mode)
                glass.view.grabFramebuffer().save(str(preview_path.with_name(f"material-{mode}.png")))
        control.minimize_settings()  # Exercise shutdown while settings are hidden.
    except Exception as exc:
        LOG.exception("Self-test failed")
        result.update(ok=False, error=str(exc))
    finally:
        calibration.close(); control.close()
    if result.get("ok"):
        assert not glass.isVisible()
        result["settings_ui"]["hidden_settings_exit"] = True
    if report_path:
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result.get("ok") else 1


def main():
    global APP_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--media-probe", action="store_true")
    parser.add_argument("--lyrics-probe", nargs=3, metavar=("TITLE", "ARTIST", "SECONDS"))
    parser.add_argument("--lyrics-album", default="", help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--startup-probe", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    application_base = application_directory(sys.executable, Path(__file__).resolve().parents[1],
        frozen=getattr(sys, "frozen", False), launcher_directory=os.environ.get("MOONLYRICS_LAUNCHER_DIRECTORY"))
    try:
        APP_DIR = data_directory(PREFERRED_APP_DIR, application_base)
    except OSError:
        message_box = api(user32, "MessageBoxW", C.c_int, W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT)
        message_box(None, "MoonLyrics 无法写入数据目录，请解压到可写文件夹后重试。\n"
                         "MoonLyrics cannot write its data folder. Extract it to a writable folder and try again.",
                    "MoonLyrics", 0x10)
        return 1
    logging.basicConfig(filename=APP_DIR / "MoonLyrics.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    LOG.info("Starting MoonLyrics; frozen=%s", getattr(sys, "frozen", False))
    if args.startup_probe:
        assets = Path(__file__).resolve().parents[1] / "assets"
        files = ("moonlyrics-icon.ico", "NotoSansSC-Black.otf", "Glasswork-LICENSE.txt", "Atrium-LICENSE.txt")
        args.startup_probe.write_text(json.dumps(dict(
            ok=all((assets / name).is_file() for name in files), version=__version__,
            executable=sys.executable, application_directory=str(application_base), data_directory=str(APP_DIR),
            bundled_assets=all((assets / name).is_file() for name in files)), ensure_ascii=False), encoding="utf-8")
        return 0
    try:
        user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    except AttributeError:
        pass
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setAlphaBufferSize(8); fmt.setDepthBufferSize(0); fmt.setStencilBufferSize(0); fmt.setSamples(0)
    fmt.setSwapInterval(0); QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication(sys.argv[:1])
    if load_bundled_font() < 0:
        LOG.warning("Bundled Noto Sans SC Black unavailable; using installed/system fonts")
    app.setApplicationName("MoonLyrics")
    app.setWindowIcon(application_icon())
    app_id = api(C.WinDLL("shell32"), "SetCurrentProcessExplicitAppUserModelID", C.c_long, W.LPCWSTR)
    app_id("mooneverun.MoonLyrics")
    app.setStyle("Fusion"); app.setFont(QFont("Microsoft YaHei UI", 10))
    if args.media_probe:
        return media_probe(app, args.report)
    if args.lyrics_probe:
        return lyrics_probe(args.lyrics_probe, args.report, args.lyrics_album, APP_DIR / "cache" / "lyrics")
    settings = dict(DEFAULTS)
    if not args.self_test:
        for directory in dict.fromkeys((APP_DIR, PREFERRED_APP_DIR, LEGACY_DIR)):
            try:
                settings = normalized(json.loads((directory / "settings.json").read_text(encoding="utf-8")))
                break
            except (OSError, ValueError, TypeError):
                continue
    try:
        control = Controls(settings, save=not args.self_test, media=not args.self_test)
        if args.self_test:
            return self_test(app, control, args.report, args.preview)
        control.show(); control.glass.show()
        return app.exec()
    except Exception as exc:
        LOG.exception("Startup failed")
        if not args.self_test: QMessageBox.critical(None, translate(settings["language"], "MoonLyrics 无法启动"), translate_message(settings["language"], str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
