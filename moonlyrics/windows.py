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

from PyQt6.QtCore import QEasingCurve, QPoint, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QSurfaceFormat, QVector2D, QVector4D
from PyQt6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                           QOpenGLShader, QOpenGLShaderProgram,
                           QOpenGLTimerQuery, QOpenGLVertexArrayObject)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QGridLayout,
                            QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
                            QSlider, QVBoxLayout, QWidget)

from .styles import DEFAULTS, LIMITS, PRESETS, LYRIC_SOURCES, normalized, transparency_amount
from .playback import LyricClock, PlaybackSnapshot, STATUS_LABELS, format_time
from .windows_media import AppleMusicReader, media_probe
from .lyrics import Lyrics, LyricLine, Track, lyrics_probe, normalize
from .lyrics_worker import LyricsWorker
from .lyric_paint import lyric_layer

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "MoonLyrics"
LEGACY_DIR = APP_DIR.parent / "LyricFloatGlassSample"
LOG = logging.getLogger("moonlyrics")
user32, gdi32, kernel32, psapi = (C.WinDLL(n, use_last_error=True)
                                    for n in ("user32", "gdi32", "kernel32", "psapi"))


def api(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


get_dc = api(user32, "GetDC", W.HDC, W.HWND)
release_dc = api(user32, "ReleaseDC", C.c_int, W.HWND, W.HDC)
client_rect = api(user32, "GetClientRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
client_origin = api(user32, "ClientToScreen", W.BOOL, W.HWND, C.POINTER(W.POINT))
set_affinity = api(user32, "SetWindowDisplayAffinity", W.BOOL, W.HWND, W.DWORD)
get_affinity = api(user32, "GetWindowDisplayAffinity", W.BOOL, W.HWND, C.POINTER(W.DWORD))
compatible_dc = api(gdi32, "CreateCompatibleDC", W.HDC, W.HDC)
delete_dc = api(gdi32, "DeleteDC", W.BOOL, W.HDC)
select_object = api(gdi32, "SelectObject", W.HANDLE, W.HDC, W.HANDLE)
delete_object = api(gdi32, "DeleteObject", W.BOOL, W.HANDLE)
bit_blt = api(gdi32, "BitBlt", W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int,
              W.HDC, C.c_int, C.c_int, W.DWORD)
gdi_flush = api(gdi32, "GdiFlush", W.BOOL)
metrics = api(user32, "GetSystemMetrics", C.c_int, C.c_int)


class BitmapInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                ("planes", W.WORD), ("bit_count", W.WORD), ("compression", W.DWORD),
                ("image_size", W.DWORD), ("x_ppm", W.LONG), ("y_ppm", W.LONG),
                ("colors_used", W.DWORD), ("colors_important", W.DWORD),
                ("color", W.DWORD)]


dib_section = api(gdi32, "CreateDIBSection", W.HBITMAP, W.HDC, C.POINTER(BitmapInfo),
                  W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD)


class DesktopRegion:
    """Keep one reusable native bitmap; capture only the glass area plus filter padding."""
    def __init__(self):
        self.screen_dc = get_dc(None)
        self.memory_dc = compatible_dc(self.screen_dc)
        if not self.screen_dc or not self.memory_dc:
            raise C.WinError(C.get_last_error())
        self.bitmap = None
        self.old_bitmap = None
        self.bits = C.c_void_p()
        self.size = (0, 0)
        self.bounds = (0., 0., 1., 1.)

    def capture(self, hwnd, pad):
        rect, origin = W.RECT(), W.POINT(0, 0)
        if not client_rect(hwnd, C.byref(rect)) or not client_origin(hwnd, C.byref(origin)):
            raise C.WinError(C.get_last_error())
        fw, fh = rect.right, rect.bottom
        width, height = fw + 2 * pad, fh + 2 * pad
        if min(fw, fh) < 1 or max(width, height) > 8192:
            raise RuntimeError("无效的桌面取样尺寸")
        if self.size != (width, height):
            if self.bitmap:
                select_object(self.memory_dc, self.old_bitmap)
                delete_object(self.bitmap)
                self.bitmap = None
            info = BitmapInfo()
            info.size, info.width, info.height = 40, width, height
            info.planes, info.bit_count = 1, 32
            self.bitmap = dib_section(self.screen_dc, C.byref(info), 0, C.byref(self.bits), None, 0)
            if not self.bitmap:
                raise C.WinError(C.get_last_error())
            self.old_bitmap = select_object(self.memory_dc, self.bitmap)
            self.size = width, height
        x, y = origin.x - pad, origin.y - pad
        vx, vy, vw, vh = (metrics(n) for n in (76, 77, 78, 79))
        sx, sy = max(x, vx), max(y, vy)
        right, bottom = min(x + width, vx + vw), min(y + height, vy + vh)
        if right <= sx or bottom <= sy:
            raise RuntimeError("玻璃条位于显示器范围外")
        if not bit_blt(self.memory_dc, sx - x, sy - y, right - sx, bottom - sy,
                       self.screen_dc, sx, sy, 0x00CC0020 | 0x40000000):
            raise C.WinError(C.get_last_error())
        gdi_flush()
        # UV coordinates are bottom-up, matching native DIB rows and OpenGL textures.
        self.bounds = ((sx - x + .5) / width, (height - (bottom - y) + .5) / height,
                       (right - x - .5) / width, (height - (sy - y) - .5) / height)
        return fw, fh, pad

    def close(self):
        if self.bitmap:
            select_object(self.memory_dc, self.old_bitmap)
            delete_object(self.bitmap)
            self.bitmap = None
        if self.memory_dc:
            delete_dc(self.memory_dc)
            self.memory_dc = None
        if self.screen_dc:
            release_dc(None, self.screen_dc)
            self.screen_dc = None


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
void main() {
    vec3 sum=vec3(0); float total=0;
    for(int i=-uRadius;i<=uRadius;i++) {
        float x=float(i)/uSigma, weight=exp(-0.5*x*x);
        vec2 p=clamp(uv+uAxis*float(i),uBounds.xy,uBounds.zw);
        sum+=texture(uSource,p).rgb*weight; total+=weight;
    }
    outputColor=vec4(sum/total,1);
}
"""
GLASS = """#version 330 core
in vec2 uv; out vec4 outputColor;
uniform sampler2D uSource;
uniform vec2 uFrameSize, uCaptureSize, uLight;
uniform vec4 uBounds;
uniform float uPadding,uDpr,uRefraction,uTransparency;
void main() {
    vec2 p=(uv-0.5)*uFrameSize;
    float radius=min(24.0*uDpr,uFrameSize.y*0.5);
    vec2 q=abs(p)-uFrameSize*0.5+radius;
    vec2 outside=max(q,0.0);
    float len=length(outside);
    float sdf=len+min(max(q.x,q.y),0.0)-radius;
    float depth=max(0.0,-sdf);
    vec2 normal=len>0.001 ? outside/len : (q.x>q.y ? vec2(1,0):vec2(0,1));
    normal*=sign(p);
    float t=depth/(23.0*uDpr);
    float profile=(t>0.0 && t<1.0) ? sin(t*3.14159265):0.0;
    vec2 offset=normal*profile*uRefraction*32.0*uDpr;
    vec2 sampleUv=(uv*uFrameSize+uPadding+offset)/uCaptureSize;
    vec3 color=texture(uSource,clamp(sampleUv,uBounds.xy,uBounds.zw)).rgb;
    color=mix(color,vec3(0.10,0.16,0.22),1.0-uTransparency);
    vec2 light=normalize(uLight+vec2(0.001));
    float rim=1.0-smoothstep(0.3*uDpr,1.5*uDpr,depth);
    float facing=0.45+0.55*max(0.0,dot(normal,light));
    color+=vec3(0.81,0.91,1.0)*(rim*facing*0.50+exp(-depth/(5.0*uDpr))*0.055);
    float visible=1.0-smoothstep(0.90,1.0,uTransparency);
    float alpha=(1.0-smoothstep(-0.7,0.7,sdf))*visible;
    outputColor=vec4(color*alpha,alpha);
}
"""


class GlassView(QOpenGLWidget):
    failed = pyqtSignal(str)

    def __init__(self, settings, capture, parent):
        super().__init__(parent)
        self.settings, self.capture = settings, capture
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.ready = False
        self.new_frame = False
        self.frame_geometry = None
        self.texture_size = (0, 0)
        self.fbos = []
        self.texture = C.c_uint(0)
        self.frames = 0
        self.capture_ms = 0.
        self.gpu_ms = None
        self.gpu_pending = False
        self.show_text = True
        self.primary_text = "等待 Apple Music"
        self.secondary_text = "播放音乐后显示歌曲与进度"
        self.lyrics = None
        self.scroll_position = -1.
        self.lyric_index = -1
        self.lyric_image_key = None
        self.dark_text = False
        self.renderer = "正在初始化显卡…"
        self.light = QVector2D(-.7, .8)
        self.transparency = transparency_amount(settings["transparency_level"])
        self.transparency_animation = QVariantAnimation(self)
        self.transparency_animation.setDuration(240)
        self.transparency_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.transparency_animation.valueChanged.connect(self.animate_transparency)
        self.frameSwapped.connect(self.swapped)

    def set_transparency(self, level):
        self.transparency_animation.stop()
        target = transparency_amount(level)
        if abs(target - self.transparency) < 1e-8:
            return
        self.transparency_animation.setStartValue(self.transparency)
        self.transparency_animation.setEndValue(target)
        self.transparency_animation.start()

    def animate_transparency(self, value):
        self.transparency = float(value)
        self.update()

    def swapped(self):
        self.frames += 1

    def set_lyrics(self, document, position=0.):
        self.lyrics = document
        self.lyric_index = document.index_at(position) if document else -1
        self.scroll_position = document.scroll_at(position) if document else -1.
        self.lyric_image_key = None
        self.update()

    def set_lyric_time(self, position):
        if self.lyrics is None:
            return
        self.lyric_index = self.lyrics.index_at(position)
        scroll = self.lyrics.scroll_at(position)
        if abs(scroll - self.scroll_position) > 1e-7:
            self.scroll_position = scroll; self.update()

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
            self.blur_program, self.glass_program = (self.program(fragment) for fragment in (BLUR, GLASS))
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

    def take_frame(self):
        started = time.perf_counter()
        dpr = self.devicePixelRatioF()
        pad = math.ceil((self.settings["blur"] * .72 + self.settings["refraction"] * .32 + 4) * dpr)
        self.frame_geometry = self.capture.capture(int(self.window().winId()), pad)
        fw, fh, _ = self.frame_geometry
        cw, ch = self.capture.size
        luminance = 0.
        for step in range(12):
            x, y = pad + (step + 1) * fw // 13, pad + fh // 2
            pixel = (C.c_ubyte * 4).from_address(self.capture.bits.value + ((ch - 1 - y) * cw + x) * 4)
            luminance += .2126 * pixel[2] + .7152 * pixel[1] + .0722 * pixel[0]
        amount = transparency_amount(self.settings["transparency_level"])
        luminance = luminance / 12 * amount + 38 * (1 - amount)
        if luminance > 165: self.dark_text = True
        elif luminance < 135: self.dark_text = False
        self.capture_ms = (time.perf_counter() - started) * 1000
        self.new_frame = True
        self.update()

    def draw(self, program, source, width, height):
        self.gl.glViewport(0, 0, width, height)
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
        if not self.frame_geometry:
            return
        if self.query_enabled and self.gpu_pending and self.query.isResultAvailable():
            self.gpu_ms = self.query.waitForResult() / 1e6
            self.gpu_pending = False
        measure = self.query_enabled and not self.gpu_pending
        if measure:
            self.query.begin()
        self.vao.bind()
        cw, ch = self.capture.size
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
            self.new_frame = False
        source = self.texture.value
        bounds = QVector4D(*self.capture.bounds)
        if self.settings["blur"] > 0:
            program = self.blur_program
            program.bind(); program.setUniformValue("uBounds", bounds)
            sigma = self.settings["blur"] * .24 * self.devicePixelRatioF()
            spacing = max(1., 3 * sigma / 128)
            program.setUniformValue("uSigma", float(sigma / spacing))
            program.setUniformValue("uRadius", min(128, math.ceil(3 * sigma / spacing)))
            for index, fbo in enumerate(self.fbos):
                fbo.bind()
                program.setUniformValue("uAxis", QVector2D(spacing / cw, 0) if index == 0 else QVector2D(0, spacing / ch))
                self.draw(program, source, fbo.width(), fbo.height())
                source = fbo.texture()
            program.release()
        self.bind_fbo(0x8D40, self.defaultFramebufferObject())
        fw, fh, pad = self.frame_geometry
        program = self.glass_program
        program.bind()
        program.setUniformValue("uFrameSize", QVector2D(fw, fh))
        program.setUniformValue("uCaptureSize", QVector2D(cw, ch))
        program.setUniformValue("uBounds", bounds)
        program.setUniformValue("uPadding", float(pad))
        program.setUniformValue("uDpr", float(self.devicePixelRatioF()))
        program.setUniformValue("uRefraction", self.settings["refraction"] / 100.)
        program.setUniformValue("uTransparency", self.transparency)
        program.setUniformValue("uLight", self.light)
        self.draw(program, source, fw, fh)
        program.release(); self.vao.release()
        if measure:
            self.query.end(); self.gpu_pending = True
        if self.show_text and self.lyrics is not None:
            key = (id(self.lyrics), self.scroll_position, self.width(), self.height(), self.devicePixelRatioF(), self.dark_text)
            if key != self.lyric_image_key:
                self.lyric_image = lyric_layer(self.lyrics, self.scroll_position, self.width(), self.height(),
                                               self.devicePixelRatioF(), self.dark_text)
                self.lyric_image_key = key
            painter = QPainter(self); painter.drawImage(0, 0, self.lyric_image); painter.end()
        elif self.show_text:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            font = QFont("Microsoft YaHei UI", 20)
            font.setWeight(QFont.Weight.Medium); painter.setFont(font)
            center = QRectF(16, self.height() / 2 - 28, self.width() - 32, 36)
            title = painter.fontMetrics().elidedText(self.primary_text, Qt.TextElideMode.ElideRight,
                                                     max(1, self.width() - 32))
            painter.setPen(QColor(255, 255, 255, 100) if self.dark_text else QColor(0, 0, 0, 150))
            painter.drawText(center.translated(0, 1), Qt.AlignmentFlag.AlignCenter, title)
            painter.setPen(QColor(24, 38, 50) if self.dark_text else QColor(250, 253, 255))
            painter.drawText(center, Qt.AlignmentFlag.AlignCenter, title)
            painter.setFont(QFont("Microsoft YaHei UI", 10))
            subtitle = painter.fontMetrics().elidedText(self.secondary_text, Qt.TextElideMode.ElideRight,
                                                        max(1, self.width() - 32))
            painter.setPen(QColor(24, 38, 50, 200) if self.dark_text else QColor(250, 253, 255, 220))
            painter.drawText(QRectF(16, self.height() / 2 + 9, self.width() - 32, 23),
                             Qt.AlignmentFlag.AlignCenter, subtitle)
            painter.end()

    def shutdown(self):
        self.transparency_animation.stop()
        if not self.ready:
            return
        self.makeCurrent()
        self.query.destroy(); self.fbos = []
        self.gl.glDeleteTextures(1, C.byref(self.texture))
        self.vbo.destroy(); self.vao.destroy()
        self.blur_program.removeAllShaders(); self.glass_program.removeAllShaders()
        self.ready = False
        self.doneCurrent()


class GlassWindow(QWidget):
    failed = pyqtSignal(str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("MoonLyrics · 月光歌词")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
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
        self.paused = False
        self.drag_anchor = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.resize(settings["width"], settings["height"])

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.protect)

    def protect(self):
        value = W.DWORD()
        hwnd = int(self.winId())
        if (not set_affinity(hwnd, 0x11) or not get_affinity(hwnd, C.byref(value))
                or value.value != 0x11):
            self.fail("当前系统无法将玻璃条排除在桌面取样之外，已停止刷新以避免画面递归。")
            return
        if not self.paused:
            self.timer.start(round(1000 / self.settings["fps"]))
        LOG.info("Desktop capture exclusion active; HWND=%s", hwnd)

    def refresh(self):
        if self.isMinimized() or not self.isVisible():
            return
        try:
            self.view.take_frame()
        except Exception as exc:
            LOG.exception("Desktop region capture failed")
            self.fail(str(exc))

    def fail(self, message):
        self.paused = True
        self.timer.stop()
        self.failed.emit(message)

    def set_locked(self, locked):
        self.timer.stop(); self.drag_anchor = None
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, locked)
        self.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_anchor = event.globalPosition().toPoint()
            self.drag_start = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        point = event.position()
        self.view.light = QVector2D(point.x() / max(1, self.width()) * 2 - 1,
                                    1 - point.y() / max(1, self.height()) * 2)
        if self.drag_anchor is not None:
            desired = self.drag_start + event.globalPosition().toPoint() - self.drag_anchor
            screen = QApplication.screenAt(event.globalPosition().toPoint()) or self.screen()
            bounds = screen.availableGeometry()
            self.move(max(bounds.left(), min(desired.x(), bounds.right() - self.width() + 1)),
                      max(bounds.top(), min(desired.y(), bounds.bottom() - self.height() + 1)))

    def mouseReleaseEvent(self, event):
        self.drag_anchor = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def closeEvent(self, event):
        self.timer.stop(); self.view.shutdown(); self.capture.close()
        super().closeEvent(event)


class Controls(QWidget):
    def __init__(self, settings, save=True, media=True):
        super().__init__()
        self.settings, self.save_enabled = settings, save
        self.setWindowTitle("MoonLyrics · 月光歌词设置")
        self.glass = GlassWindow(settings)
        self.glass.failed.connect(self.failed)
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
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self); layout.setContentsMargins(22, 20, 22, 20); layout.setSpacing(12)
        title = QLabel("MoonLyrics · 月光歌词"); title.setStyleSheet("font-size: 20px; font-weight: 500;")
        layout.addWidget(title)
        help_text = QLabel("播放 Apple Music，歌词会在玻璃框内居中滚动、上下渐隐。\n按住悬浮条可拖动；锁定后允许鼠标穿透。")
        help_text.setWordWrap(True); layout.addWidget(help_text)
        self.track_title = QLabel("等待 Apple Music")
        self.track_title.setWordWrap(True)
        self.track_title.setTextFormat(Qt.TextFormat.PlainText)
        self.track_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.track_title.setStyleSheet("font-size: 16px; font-weight: 500;")
        self.track_details = QLabel("打开 Apple Music 并播放一首歌曲")
        self.track_details.setWordWrap(True); self.track_details.setTextFormat(Qt.TextFormat.PlainText)
        self.track_details.setStyleSheet("color: #66778a;")
        layout.addWidget(self.track_title); layout.addWidget(self.track_details)
        media_row = QHBoxLayout()
        self.media_status = QLabel("等待 Apple Music"); self.media_time = QLabel("0:00 / --:--")
        media_row.addWidget(self.media_status); media_row.addStretch(); media_row.addWidget(self.media_time)
        layout.addLayout(media_row)
        self.media_progress = QProgressBar(); self.media_progress.setRange(0, 1000)
        self.media_progress.setTextVisible(False); self.media_progress.setFixedHeight(6)
        self.media_progress.setValue(0); self.media_progress.setAccessibleName("Apple Music 播放进度")
        layout.addWidget(self.media_progress)
        lyric_row = QHBoxLayout()
        lyric_row.addWidget(QLabel("曲目地区")); self.storefront = QComboBox()
        for code, label in (("tr", "土耳其"), ("cn", "中国大陆"), ("hk", "香港"), ("tw", "台湾"), ("us", "美国")):
            self.storefront.addItem(label, code)
        self.storefront.setCurrentIndex(self.storefront.findData(settings["storefront"]))
        self.storefront.setToolTip("仅用于跨地区曲名匹配，不会修改 Apple Music 账号地区。")
        self.storefront.currentIndexChanged.connect(self.storefront_changed)
        lyric_row.addWidget(self.storefront); lyric_row.addStretch()
        self.retry_lyrics = QPushButton("重新匹配"); self.retry_lyrics.setEnabled(False)
        self.retry_lyrics.clicked.connect(lambda: self.request_lyrics(force=True))
        lyric_row.addWidget(self.retry_lyrics); layout.addLayout(lyric_row)
        source_row = QHBoxLayout(); source_row.setSpacing(4)
        source_row.addWidget(QLabel("歌词源顺序"))
        self.source_choices = []
        for position, source in enumerate(settings["lyric_source_order"]):
            if position:
                source_row.addWidget(QLabel("→"))
            choice = QComboBox()
            for key, label in LYRIC_SOURCES.items():
                choice.addItem(label, key)
            choice.setCurrentIndex(choice.findData(source))
            choice.setAccessibleName(f"第 {position + 1} 优先歌词源")
            choice.setToolTip("按从左到右的顺序自动匹配，未找到时继续尝试下一家。\n选择已有来源会交换位置，并重新匹配当前歌曲。")
            choice.currentIndexChanged.connect(lambda index, slot=position: self.source_order_changed(slot))
            self.source_choices.append(choice)
            source_row.addWidget(choice)
        layout.addLayout(source_row)
        self.lyrics_status = QLabel("播放歌曲后自动匹配同步歌词")
        self.lyrics_status.setWordWrap(True); layout.addWidget(self.lyrics_status)
        timing_row = QHBoxLayout(); timing_row.addWidget(QLabel("歌词校准"))
        earlier = QPushButton("提前 0.1 秒"); later = QPushButton("延后 0.1 秒")
        reset_timing = QPushButton("重置")
        earlier.clicked.connect(lambda: self.change_lyric_offset(100))
        later.clicked.connect(lambda: self.change_lyric_offset(-100))
        reset_timing.clicked.connect(lambda: self.change_lyric_offset(-self.settings["lyric_offset_ms"]))
        self.lyric_timing = QLabel(); self.lyric_timing.setMinimumWidth(80)
        self.lyric_timing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lyric_timing.setText(self.offset_label())
        self.lyric_timing.setToolTip("歌词慢了点提前，歌词快了点延后；只校准歌词，不改变歌曲播放进度。")
        for widget in (earlier, self.lyric_timing, later, reset_timing): timing_row.addWidget(widget)
        layout.addLayout(timing_row)
        layout.addWidget(QLabel("玻璃风格"))
        preset_row = QHBoxLayout()
        self.preset_group = QButtonGroup(self)
        self.preset_group.setExclusive(True)
        self.preset_buttons = {}
        for key, preset in PRESETS.items():
            button = QPushButton(preset["label"])
            button.setCheckable(True); button.setChecked(key == settings["preset"])
            button.setAccessibleName(preset["label"] + "玻璃风格")
            button.setToolTip(preset["description"])
            button.setStyleSheet("QPushButton {padding: 9px 14px; border: 1px solid #b8c4d0; border-radius: 7px;} QPushButton:checked {background: #dcebf9; color: #173750; border-color: #7faace;}")
            button.clicked.connect(lambda checked, name=key: self.set_preset(name))
            self.preset_group.addButton(button)
            self.preset_buttons[key] = button
            preset_row.addWidget(button)
        layout.addLayout(preset_row)
        self.preset_description = QLabel(PRESETS[settings["preset"]]["description"])
        self.preset_description.setWordWrap(True); layout.addWidget(self.preset_description)
        grid = QGridLayout(); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(14)
        labels = dict(transparency_level="背景透明度", width="玻璃条宽度", height="玻璃条高度")
        for row, (key, label) in enumerate(labels.items()):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(*LIMITS[key]); slider.setValue(settings[key])
            slider.setAccessibleName(label)
            value = QLabel(); value.setMinimumWidth(56); value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            caption = QLabel(label); caption.setBuddy(slider)
            def change(number, field=key, readout=value):
                self.settings[field] = number
                readout.setText(f"{number}{' px' if field in ('width', 'height') else ' / 10'}")
                if field == "transparency_level":
                    self.glass.view.set_transparency(number)
                else:
                    self.glass.resize(self.settings["width"], self.settings["height"])
                self.glass.refresh()
            slider.valueChanged.connect(change); change(settings[key])
            self.sliders[key] = slider
            grid.addWidget(caption, row, 0); grid.addWidget(slider, row, 1); grid.addWidget(value, row, 2)
        layout.addLayout(grid)
        self.sliders["transparency_level"].setToolTip("1 保留更多磨砂，10 完全透明；切换时柔和过渡。")
        row = QHBoxLayout()
        row.addWidget(QLabel("刷新上限")); self.fps = QComboBox()
        for value in (15, 30, 60): self.fps.addItem(f"{value} FPS", value)
        self.fps.setCurrentIndex((15, 30, 60).index(settings["fps"]))
        self.fps.currentIndexChanged.connect(self.fps_changed); row.addWidget(self.fps)
        row.addStretch(); self.pause = QPushButton("暂停刷新"); self.pause.clicked.connect(self.toggle_pause); row.addWidget(self.pause)
        layout.addLayout(row)
        self.lock = QCheckBox("锁定悬浮条，允许鼠标穿透（在这里解锁）")
        self.lock.toggled.connect(self.glass.set_locked); layout.addWidget(self.lock)
        self.stats = QLabel("正在测量 CPU、内存与实际帧率…"); self.stats.setWordWrap(True); layout.addWidget(self.stats)
        self.timing = QLabel(); self.timing.setWordWrap(True); layout.addWidget(self.timing)
        self.hardware = QLabel("正在识别显卡…"); self.hardware.setWordWrap(True); layout.addWidget(self.hardware)
        footer = QLabel("桌面画面仅在本机处理，不保存、不上传。\n歌词匹配会向曲目和歌词服务查询歌名、歌手与时长。\nCPU 为整机归一化占比；GPU 显示材质绘制耗时。")
        footer.setWordWrap(True); footer.setStyleSheet("color: #66778a; font-size: 12px;"); layout.addWidget(footer)
        self.status = QLabel("拖动后方的网页或播放视频，可检验真实背景取样。")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.sample_timer = QTimer(self); self.sample_timer.timeout.connect(self.sample); self.sample_timer.start(1000)
        screen = QApplication.primaryScreen().availableGeometry()
        self.glass.move(screen.center().x() - settings["width"] // 2, screen.top() + 160)
        self.adjustSize()
        self.move(screen.left() + 40, max(screen.top() + 20, screen.bottom() - self.height() - 30))
        self.media_timer = QTimer(self); self.media_timer.timeout.connect(self.update_playback_display)
        self.media_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.media_timer.start(33)
        if media:
            self.lyrics_worker = LyricsWorker(self)
            self.lyrics_worker.result_ready.connect(self.accept_lyrics)
            self.lyrics_worker.start()
            self.media_reader = AppleMusicReader(self)
            self.media_reader.snapshot_changed.connect(self.set_playback)
            self.media_reader.start()

    def set_playback(self, snapshot):
        if self.closing:
            return
        self.playback = snapshot
        self.lyric_clock.update(snapshot)
        self.track_title.setText(snapshot.title or "等待 Apple Music")
        self.track_details.setText(" · ".join(value for value in (snapshot.artist, snapshot.album) if value)
                                   or "打开 Apple Music 并播放一首歌曲")
        self.media_status.setToolTip(snapshot.message)
        if (self.matching_key(snapshot) != self.lyric_key
                or snapshot.duration > 0 and abs(snapshot.duration - self.lyric_duration) > 1.):
            self.request_lyrics(defer=True)
        self.update_playback_display()

    def storefront_changed(self):
        self.settings["storefront"] = self.storefront.currentData()
        self.request_lyrics(force=True)

    def source_order_changed(self, position):
        order = list(self.settings["lyric_source_order"])
        selected = self.source_choices[position].currentData()
        previous = order.index(selected)
        order[previous], order[position] = order[position], selected
        self.settings["lyric_source_order"] = tuple(order)
        for choice, source in zip(self.source_choices, order):
            blocked = choice.blockSignals(True)
            choice.setCurrentIndex(choice.findData(source))
            choice.blockSignals(blocked)
        self.request_lyrics(force=True)

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
        self.lyrics_status.setText(("等待歌曲信息稳定…" if defer else "正在匹配歌词…") if valid else "等待完整的歌曲信息")
        track = Track(snapshot.title, snapshot.artist, snapshot.album, self.lyric_duration) if valid else None
        if self.lyrics_worker is not None:
            self.lyrics_worker.request(self.lyric_request, None if defer else track, self.settings["storefront"], force,
                                       self.settings["lyric_source_order"])
        if defer and valid:
            self.lyric_debounce.start(600)

    def accept_lyrics(self, request_id, document, message):
        if self.closing or request_id != self.lyric_request:
            return
        self.lyrics_status.setText(message)
        self.glass.view.set_lyrics(document, self.lyric_clock.position_at() + self.settings["lyric_offset_ms"] / 1000)
        if document is not None:
            self.lyrics_status.setToolTip(f"{document.title} — {document.artist} · {document.source}")
        self.update_playback_display()

    def offset_label(self):
        offset = self.settings["lyric_offset_ms"]
        return f"{'提前' if offset > 0 else '延后'} {abs(offset) / 1000:.1f} 秒" if offset else "默认时序"

    def change_lyric_offset(self, delta):
        lo, hi = LIMITS["lyric_offset_ms"]
        self.settings["lyric_offset_ms"] = max(lo, min(hi, self.settings["lyric_offset_ms"] + delta))
        self.lyric_timing.setText(self.offset_label())
        self.update_playback_display()

    def update_playback_display(self):
        snapshot = self.playback
        position = snapshot.position_at()
        state = STATUS_LABELS.get(snapshot.status, "等待播放")
        timing = format_time(position) + " / " + (format_time(snapshot.duration) if snapshot.duration else "--:--")
        self.media_status.setText(state); self.media_time.setText(timing)
        self.media_progress.setValue(round(position / snapshot.duration * 1000) if snapshot.duration else 0)
        self.glass.view.set_lyric_time(self.lyric_clock.position_at() + self.settings["lyric_offset_ms"] / 1000)
        primary = " — ".join(value for value in (snapshot.title, snapshot.artist) if value) or state
        secondary = timing + " · " + self.lyrics_status.text() if snapshot.title else "播放音乐后显示歌曲与进度"
        if (primary, secondary) != (self.glass.view.primary_text, self.glass.view.secondary_text):
            self.glass.view.primary_text, self.glass.view.secondary_text = primary, secondary
            self.glass.view.update()

    def set_preset(self, name):
        preset = PRESETS[name]
        self.settings.update(preset=name, blur=preset["blur"], refraction=preset["refraction"])
        self.preset_buttons[name].setChecked(True)
        self.preset_description.setText(preset["description"])
        self.glass.refresh()

    def fps_changed(self):
        self.settings["fps"] = self.fps.currentData()
        if not self.glass.paused:
            self.glass.timer.start(round(1000 / self.settings["fps"]))

    def toggle_pause(self):
        self.glass.paused = not self.glass.paused
        if self.glass.paused:
            self.glass.timer.stop()
        else:
            self.glass.protect()
        self.pause.setText("继续刷新" if self.glass.paused else "暂停刷新")

    def failed(self, message):
        self.error = message
        self.status.setText("已停止刷新：" + message)
        self.status.setStyleSheet("color: #b43c43;")
        self.pause.setText("重试刷新")

    def sample(self):
        cpu, memory = self.usage.read()
        now = time.perf_counter(); view = self.glass.view
        fps = (view.frames - self.last_frames) / max(.01, now - self.last_sample_time)
        self.last_frames, self.last_sample_time = view.frames, now
        sample = dict(cpu=cpu, memory_mb=memory, fps=fps, capture_ms=view.capture_ms, gpu_ms=view.gpu_ms)
        self.samples.append(sample)
        self.samples = self.samples[-120:]
        self.stats.setText(f"CPU  {cpu:.2f}%     内存  {memory:.1f} MB     实际帧率  {fps:.1f} FPS")
        gpu = "—" if view.gpu_ms is None else f"{view.gpu_ms:.2f} ms"
        self.timing.setText(f"桌面取样  {view.capture_ms:.2f} ms / 帧     GPU 材质绘制  {gpu}")
        self.hardware.setText("显卡：" + view.renderer)

    def closeEvent(self, event):
        self.closing = True
        self.lyric_debounce.stop()
        readers = [reader for reader in (self.media_reader, self.lyrics_worker) if reader is not None]
        for reader in readers:
            reader.stop()
        if any(not reader.wait(100) for reader in readers):
            self.lyrics_status.setText("正在结束后台请求…")
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


def self_test(app, control, report_path, preview_path):
    from PyQt6.QtTest import QTest
    from PyQt6.QtGui import QImage
    from PIL import Image, ImageChops, ImageFilter, ImageStat

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

    def parameters(**values):
        for key, number in values.items():
            if key == "fps": control.fps.setCurrentIndex((15, 30, 60).index(number))
            elif key == "preset": control.preset_buttons[number].click()
            elif key in ("blur", "refraction"): control.settings[key] = number
            else: control.sliders[key].setValue(number)
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
        assert control.sliders["transparency_level"].minimum() == 1
        assert control.sliders["transparency_level"].maximum() == 10
        assert "blur" not in control.sliders and "refraction" not in control.sliders
        assert normalized(dict(transparency_level=0))["transparency_level"] == 1
        assert normalized(dict(transparency_level=11))["transparency_level"] == 10
        assert normalized(dict(transparency=90))["transparency_level"] == 1
        assert normalized(dict(transparency=100))["transparency_level"] == 10
        assert normalized(dict(transparency_level=4, transparency=90))["transparency_level"] == 4
        assert transparency_amount(1) == .9 and transparency_amount(10) == 1.
        for name, preset in PRESETS.items():
            restored = normalized(dict(preset=name, blur=100, refraction=100, width=640, transparency=96))
            assert restored["preset"] == name and restored["blur"] == preset["blur"] and restored["refraction"] == preset["refraction"]
            assert normalized(json.loads(json.dumps(restored))) == restored
        assert normalized(dict(preset=[]))["preset"] == DEFAULTS["preset"]
        result["preset_settings_and_transparency_bounds"] = True
        assert glass.view.ready, "GPU renderer did not initialize"
        assert not control.error, control.error
        glass.view.show_text = False
        parameters(blur=15, refraction=24, transparency_level=1)
        glass.timer.stop(); glass.hide(); QTest.qWait(120)
        geometry = glass.view.frame_geometry
        glass.capture.capture(int(glass.winId()), geometry[2])
        baseline = C.string_at(glass.capture.bits, math.prod(glass.capture.size) * 4)
        raw_image = Image.frombytes("RGB", glass.capture.size, baseline, "raw", "BGRX", 0, -1)
        colors = sorted(raw_image.getcolors(raw_image.width * raw_image.height), reverse=True)
        result["calibration_dominant_colors"] = colors[:2]
        assert all(color in [(41, 126, 187), (242, 192, 112)] for _, color in colors[:2]), "Calibration window is obscured"
        glass.show(); QTest.qWait(200); glass.timer.stop()
        glass.capture.capture(int(glass.winId()), geometry[2])
        after = C.string_at(glass.capture.bits, len(baseline))
        difference = sum(abs(a - b) for a, b in zip(baseline, after)) / len(baseline)
        assert difference < .05, f"Capture includes overlay or changing background: {difference}"
        result["capture_exclusion_mean_byte_difference"] = difference
        parameters(blur=0, refraction=0); sharp = image()
        fw, fh, pad = glass.view.frame_geometry
        raw_frame = Image.frombytes("RGB", glass.capture.size,
                                    C.string_at(glass.capture.bits, math.prod(glass.capture.size) * 4),
                                    "raw", "BGRX", 0, -1).crop((pad, pad, pad + fw, pad + fh))
        expected = Image.merge("RGB", [channel.point(lambda v, t=tint: round(v * .9 + t * 255 * .1))
                                      for channel, tint in zip(raw_frame.split(), (.10, .16, .22))])
        center_crop = (20, round(fh * .35), fw - 20, round(fh * .65))
        alignment_error = sum(ImageStat.Stat(ImageChops.difference(sharp, expected).crop(center_crop)).mean) / 3
        result["zero_effect_alignment_mean_error"] = alignment_error
        assert alignment_error < 4, f"Background sampling alignment/orientation failed: {alignment_error}"
        parameters(blur=100, refraction=0); blurred = image()
        parameters(blur=0, refraction=100); refracted = image()
        crop = (10, 10, sharp.width - 10, sharp.height - 10)
        def energy(im):
            return sum(ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES).crop(crop)).mean)
        result["blur_edge_energy_ratio"] = energy(blurred) / energy(sharp)
        result["refraction_mean_pixel_difference"] = sum(ImageStat.Stat(ImageChops.difference(sharp, refracted)).mean) / 3
        assert result["blur_edge_energy_ratio"] < .35, result
        assert result["refraction_mean_pixel_difference"] > .5, result
        preset_images = []
        for name, preset in PRESETS.items():
            before = {key: control.settings[key] for key in ("width", "height", "transparency_level")}
            parameters(preset=name)
            assert control.settings["blur"] == preset["blur"] and control.settings["refraction"] == preset["refraction"]
            assert sum(button.isChecked() for button in control.preset_buttons.values()) == 1
            assert all(control.settings[key] == value for key, value in before.items())
            preset_images.append(image())
        result["preset_pixel_differences"] = [sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
                                               for a, b in zip(preset_images, preset_images[1:])]
        assert all(value > .5 for value in result["preset_pixel_differences"]), result
        if preview_path:
            comparison = Image.new("RGB", (preset_images[0].width, sum(im.height for im in preset_images)))
            offset = 0
            for im in preset_images:
                comparison.paste(im, (0, offset)); offset += im.height
            comparison.save(preview_path.with_name("glass-presets.png"))
        # Fine stripes expose sparse-kernel aliasing and nearest-filter blocks that a coarse checker misses.
        result["frost_stripe_checks"] = []
        for direction in ("vertical", "horizontal"):
            calibration.pattern = direction
            for period in (2, 3, 4, 6, 8):
                calibration.period = period; calibration.update(); QTest.qWait(50)
                for name in ("soft", "rich"):
                    parameters(preset=name)
                    actual = image()
                    cw, ch = glass.capture.size
                    fw, fh, pad = glass.view.frame_geometry
                    raw = Image.frombytes("RGB", (cw, ch),
                                          C.string_at(glass.capture.bits, cw * ch * 4), "raw", "BGRX", 0, -1)
                    reference = raw.filter(ImageFilter.GaussianBlur(PRESETS[name]["blur"] * .24 * glass.devicePixelRatioF()))
                    reference = reference.crop((pad, pad, pad + fw, pad + fh))
                    reference = Image.merge("RGB", [channel.point(lambda v, t=tint: round(v * .9 + t * 255 * .1))
                                                     for channel, tint in zip(reference.split(), (.10, .16, .22))])
                    center = (round(fw * .2), round(fh * .35), round(fw * .8), round(fh * .65))
                    error = sum(ImageStat.Stat(ImageChops.difference(actual, reference).crop(center)).mean) / 3
                    variation = max(ImageStat.Stat(actual.crop(center)).stddev)
                    result["frost_stripe_checks"].append(dict(direction=direction, period=period, preset=name,
                                                            reference_error=error, variation=variation))
                    assert error < 2 and variation < 1.5, result["frost_stripe_checks"][-1]
        calibration.pattern = "checker"; calibration.update(); QTest.qWait(80)
        # The animation must also repaint when desktop refresh is paused, and retarget without jumping.
        glass.timer.stop()
        samples = []
        glass.view.transparency_animation.valueChanged.connect(lambda value: samples.append(float(value)))
        before_frames = glass.view.frames
        control.sliders["transparency_level"].setValue(10)
        QTest.qWait(70)
        mid = glass.view.transparency
        assert .9 < mid < 1. and glass.view.frames > before_frames
        QTest.qWait(230)
        assert abs(glass.view.transparency - 1.) < 1e-8
        assert len(set(samples)) >= 4 and all(b >= a for a, b in zip(samples, samples[1:]))
        assert not glass.timer.isActive()
        control.sliders["transparency_level"].setValue(1); QTest.qWait(60)
        prior = glass.view.transparency
        control.sliders["transparency_level"].setValue(5)
        assert abs(glass.view.transparency - prior) < 1e-8
        QTest.qWait(300)
        assert abs(glass.view.transparency - transparency_amount(5)) < 1e-8
        result["transparency_smooth_transition"] = dict(intermediate=mid, distinct_values=len(set(samples)),
                                                       paused_refresh=True, continuous_retarget=True)
        # Match input/output at blur=0 so flips, incorrect coordinates and stale textures cannot pass.
        parameters(blur=0, refraction=0, transparency_level=10)
        transparent = glass.view.grabFramebuffer()
        assert transparent.pixelColor(transparent.width() // 2, transparent.height() // 2).alpha() == 0
        result["transparent_background_alpha"] = 0
        glass.view.show_text = True
        result["profiles"] = {}
        for name, values in (("soft_30fps", dict(preset="soft", transparency_level=1, fps=30)),
                             ("maximum_60fps", dict(blur=100, refraction=100, transparency_level=1, fps=60))):
            parameters(**values); glass.timer.start(round(1000 / values["fps"]))
            QTest.qWait(1200); control.samples.clear()
            for _ in range(7): QTest.qWait(1000)
            samples = list(control.samples)
            result["profiles"][name] = {key: statistics.mean(s[key] for s in samples if s[key] is not None)
                                        for key in samples[0] if any(s[key] is not None for s in samples)}
        parameters(preset="soft", transparency_level=1, width=583, height=80, fps=30)
        control.set_playback(PlaybackSnapshot(title="月光轻轻落在窗台", artist="MoonLyrics",
                                             album="播放信息测试", status="paused", position=64., duration=270.))
        assert control.media_time.text() == "1:04 / 4:30"
        assert control.media_status.text() == "已暂停"
        assert control.media_progress.value() == round(64 / 270 * 1000)
        assert "月光轻轻落在窗台" in glass.view.primary_text
        result["playback_display"] = True
        original_order = control.settings["lyric_source_order"]
        control.source_choices[0].setCurrentIndex(control.source_choices[0].findData("qq"))
        assert control.settings["lyric_source_order"][0] == "qq"
        assert len(set(choice.currentData() for choice in control.source_choices)) == len(LYRIC_SOURCES)
        assert normalized(json.loads(json.dumps(control.settings)))["lyric_source_order"] == control.settings["lyric_source_order"]
        for choice, source in zip(control.source_choices, original_order):
            choice.setCurrentIndex(choice.findData(source))
        assert control.settings["lyric_source_order"] == original_order
        result["source_priority_controls"] = True
        assert normalize("遠走高飛") == normalize("远走高飞")
        control.media_timer.stop()
        control.lyric_debounce.stop()
        document = Lyrics((LyricLine(60., "上面一行轻轻隐去"), LyricLine(64., "中间一行清晰显示"),
                           LyricLine(68., "下面一行渐渐出现"), LyricLine(72., "下一句等待时光")), "测试")
        control.lyric_request = 8
        control.accept_lyrics(7, document, "旧请求")
        assert glass.view.lyrics is None, "An old song replaced the current lyrics"
        control.accept_lyrics(8, document, "已匹配 · 测试")
        assert glass.view.lyric_index == 1
        glass.view.set_lyric_time(67.725)
        assert abs(glass.view.scroll_position - 1.5) < 1e-7, "Lyric movement did not ease in before the cue"
        glass.view.set_lyric_time(68.)
        assert glass.view.scroll_position == 2.
        from unittest.mock import patch
        base = time.perf_counter()
        scroll_samples = []
        def playback_at(position, elapsed, status="playing"):
            with patch("time.perf_counter", return_value=base + elapsed):
                control.set_playback(PlaybackSnapshot(title="月光轻轻落在窗台", artist="MoonLyrics",
                                                     album="播放信息测试", status=status, position=position,
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
        glass.view.update(); QTest.qWait(100)
        result["renderer"] = glass.view.renderer
        result["device_pixel_ratio"] = glass.devicePixelRatioF()
        result["ok"] = True
        if preview_path:
            glass.view.grabFramebuffer().save(str(preview_path))
            control.grab().save(str(preview_path.with_name("glass-controls.png")))
    except Exception as exc:
        LOG.exception("Self-test failed")
        result.update(ok=False, error=str(exc))
    finally:
        calibration.close(); control.close()
    if report_path:
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result.get("ok") else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--media-probe", action="store_true")
    parser.add_argument("--lyrics-probe", nargs=3, metavar=("TITLE", "ARTIST", "SECONDS"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=APP_DIR / "MoonLyrics.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    LOG.info("Starting MoonLyrics; frozen=%s", getattr(sys, "frozen", False))
    try:
        user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    except AttributeError:
        pass
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setAlphaBufferSize(8); fmt.setDepthBufferSize(0); fmt.setStencilBufferSize(0); fmt.setSamples(0)
    fmt.setSwapInterval(0); QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("MoonLyrics")
    app.setStyle("Fusion"); app.setFont(QFont("Microsoft YaHei UI", 10))
    if args.media_probe:
        return media_probe(app, args.report)
    if args.lyrics_probe:
        return lyrics_probe(args.lyrics_probe, args.report)
    settings = dict(DEFAULTS)
    if not args.self_test:
        for directory in (APP_DIR, LEGACY_DIR):
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
        if not args.self_test: QMessageBox.critical(None, "MoonLyrics 无法启动", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
