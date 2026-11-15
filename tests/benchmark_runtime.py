"""Opt-in native runtime comparison with fake playback and a controlled desktop.

Run with the existing Python 3.12 runtime and --source pointing to a checkout or
saved baseline. Never changes the user's music, preferences, wallpaper or EXE.
"""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
parser.add_argument('--seconds', type=float, default=7)
args = parser.parse_args()
sys.path.insert(0, str(args.source.resolve()))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QSurfaceFormat
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget
from moonlyrics import __version__, windows as native
from moonlyrics.lyrics import Lyrics, LyricLine
from moonlyrics.playback import PlaybackSnapshot
from moonlyrics.styles import normalized

native.user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
fmt = QSurfaceFormat()
fmt.setVersion(3, 3); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
fmt.setAlphaBufferSize(8); fmt.setDepthBufferSize(0); fmt.setStencilBufferSize(0)
fmt.setSamples(0); fmt.setSwapInterval(0); QSurfaceFormat.setDefaultFormat(fmt)
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
app.setStyle('Fusion'); app.setFont(QFont('Microsoft YaHei UI', 10))
native.load_bundled_font()


class Background(QWidget):
    phase = 0
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#658fba'))
        for x in range(-40, self.width()+40, 36):
            painter.fillRect(x+self.phase, 0, 16, self.height(), QColor('#d1c8aa'))
        for y in range(10, self.height(), 26):
            painter.fillRect(0, y, self.width(), 2, QColor('#334a62'))
        painter.end()


background = Background()
background.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
background.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
bounds = app.primaryScreen().availableGeometry()
background.setGeometry(bounds.left()+40, bounds.top()+100, 680, 260)
background.show()
control = native.Controls(normalized(dict(width=500, height=80, fps=120, appearance_mode='day')),
                          save=False, media=False)
control.move(bounds.right()-control.width()-20, bounds.top()+20)
settings_background = Background()
settings_background.setWindowFlags(Qt.WindowType.FramelessWindowHint)
settings_background.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
settings_background.setGeometry(control.geometry())
glass = control.glass
glass.move(background.x()+50, background.y()+90)
glass.show()
QTest.qWait(800)
assert glass.view.ready and not control.error, control.error
view = glass.view
counts = dict(captures=0, settings_captures=0, blur_passes=0, uploads=0, settings_updates=0)
original_capture, original_draw = glass.capture.capture, view.draw
original_upload = view.gl.glTexSubImage2D
original_progress = control.media_progress.setValue
original_settings_capture = control.settings_backdrop.capture.capture

def capture(*a, **k):
    counts['captures'] += 1
    return original_capture(*a, **k)
def draw(program, *a, **k):
    counts['blur_passes'] += int(program is view.blur_program)
    return original_draw(program, *a, **k)
def upload(*a):
    counts['uploads'] += 1
    return original_upload(*a)
def progress(value):
    counts['settings_updates'] += 1
    return original_progress(value)
def settings_capture(*a, **k):
    counts['settings_captures'] += 1
    return original_settings_capture(*a, **k)
glass.capture.capture, view.draw = capture, draw
view.gl.glTexSubImage2D = upload
control.media_progress.setValue = progress
control.settings_backdrop.capture.capture = settings_capture

background_timer = QTimer()
def move_background():
    background.phase = (background.phase+3) % 36
    background.update()
background_timer.timeout.connect(move_background)
heartbeat = QTimer()
scene_started = 0.
playing = False
def update_playback():
    control.set_playback(PlaybackSnapshot(source='benchmark', title='Controlled lyrics', artist='MoonLyrics',
        status='playing', duration=180, position=1+time.perf_counter()-scene_started,
        observed_at=time.perf_counter()))
heartbeat.timeout.connect(update_playback)
document = Lyrics(tuple(LyricLine(index*10, '月光落在窗台，把这一刻慢慢收藏，晚风经过安静的街道，歌声一直陪伴着我们。')
                        for index in range(18)), 'benchmark')
report = dict(version=__version__, fps_limit=120, width=500, height=80,
              renderer=view.renderer, scenarios={})

try:
    for name, playing, moving, visible in (
        ('idle_hidden_static', False, False, False),
        ('playing_hidden_static', True, False, False),
        ('playing_hidden_motion', True, True, False),
        ('playing_visible_static', True, False, True),
        ('playing_hidden_after_settings', True, False, False),
    ):
        heartbeat.stop(); background_timer.stop()
        scene_started = time.perf_counter()
        background.phase = 0; background.update()
        if visible:
            settings_background.show();control.show();control.raise_()
        else:
            control.hide();settings_background.hide()
        if playing:
            update_playback(); control.lyric_debounce.stop(); view.set_lyrics(document, 1.)
            heartbeat.start(500)
        else:
            control.set_playback(PlaybackSnapshot())
        if moving: background_timer.start(16)
        QTest.qWait(4200 if name == 'playing_hidden_after_settings' else 1800)
        assert not playing or view.lyrics is document, 'The synthetic lyrics were replaced by a deferred search'
        for key in counts: counts[key] = 0
        frames = view.frames
        started, cpu = time.perf_counter(), native.Usage.cpu_time()
        QTest.qWait(round(args.seconds*1000))
        elapsed = time.perf_counter()-started
        assert not playing or view.lyrics is document, 'The lyric scenario did not retain its document'
        memory = native.MemoryCounters(); memory.cb = C.sizeof(memory)
        assert native.memory_info(native.process_handle, C.byref(memory), memory.cb)
        sample = dict(cpu_percent=(native.Usage.cpu_time()-cpu)/elapsed/(os.cpu_count() or 1)*100,
                      working_set_mb=memory.working_set/1048576, private_commit_mb=memory.private/1048576,
                      rendered_fps=(view.frames-frames)/elapsed, capture_calls_per_s=counts['captures']/elapsed,
                      blur_passes_per_s=counts['blur_passes']/elapsed, gpu_uploads_per_s=counts['uploads']/elapsed,
                      capture_timer_ms=glass.timer.interval(), media_timer_active=control.media_timer.isActive())
        sample['settings_capture_calls_per_s'] = counts['settings_captures']/elapsed
        sample['settings_frame_released'] = getattr(control.settings_backdrop,'released',False)
        sample['settings_updates_per_s'] = counts['settings_updates']/elapsed
        sample['lyrics_present'] = view.lyrics is not None
        assert not control.error, control.error
        report['scenarios'][name] = sample
        print(name, json.dumps(sample), flush=True)
finally:
    heartbeat.stop(); background_timer.stop()
    control.close(); background.close();settings_background.close()
args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
