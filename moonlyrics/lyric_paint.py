"""A clipped, vertically faded lyric layer; the glass itself is left untouched."""
import math
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath
from .styles import DEFAULTS


def load_bundled_font():
    return QFontDatabase.addApplicationFont(str(Path(__file__).resolve().parents[1] / "assets" / "NotoSansSC-Black.otf"))


def lyric_font(family):
    family = family or DEFAULTS["lyric_font"]
    font = QFont(family)
    font.setPixelSize(26)
    font.setWeight(QFont.Weight.Black if family.casefold() == "noto sans sc black" else QFont.Weight.Medium)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


@lru_cache(maxsize=128)
def text_width(text, family):
    return QFontMetricsF(lyric_font(family)).horizontalAdvance(text)


def lyric_pan(document, position, width, family, end_time=None):
    index = document.index_at(position) if document else -1
    if index < 0:
        return index, 0.
    excess = text_width(document.lines[index].text, family) - (width - 48)
    if excess <= 0:
        return index, 0.
    cue = document.lines[index].time
    if index + 1 < len(document.lines):
        duration = document.lines[index + 1].time - cue
    elif end_time is not None and math.isfinite(end_time) and end_time > cue:
        duration = end_time - cue
    else:
        duration = max(6., excess / 45 + 1.2)
    hold, tail = min(.6, duration * .12), min(.8, duration * .2)
    progress = max(0., min(1., (position - cue - hold) / max(.01, duration - hold - tail)))
    return index, progress * progress * (3 - 2 * progress)


def lyric_layer(document, scroll, width, height, dpr, dark, family=None, pan=None):
    image = QImage(math.ceil(width * dpr), math.ceil(height * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    clip = QPainterPath(); clip.addRoundedRect(QRectF(0, 0, width, height), min(24., height / 2), min(24., height / 2))
    painter.setClipPath(clip)
    available = max(1., width - 48)
    painter.setClipRect(QRectF(24, 0, available, height), Qt.ClipOperation.IntersectClip)
    spacing = max(28., min(46., height * .35))
    font = lyric_font(family)
    painter.setFont(font)
    metrics = QFontMetricsF(font)
    active, progress = pan if pan is not None else (math.floor(scroll), 0.)
    start, end = math.floor(scroll) - 1, math.ceil(scroll) + 2
    for index in range(max(-1, start), min(len(document.lines), end)):
        y = height / 2 + (index - scroll) * spacing
        if y < -20 or y > height + 20:
            continue
        text = document.lines[index].text if index >= 0 else "♪"
        text = text or "♪"
        emphasis = max(0., 1 - abs(index - scroll))
        emphasis = emphasis * emphasis * (3 - 2 * emphasis)
        advance = text_width(text, family)
        scale = .84 + .16 * emphasis
        overflow = max(0., advance * scale - available)
        phase = progress if index == active else float(index < active)
        x = -available / 2 - overflow * phase if overflow else -advance * scale / 2
        # Keep the font size stable; long lines travel with playback instead of shrinking.
        painter.save(); painter.translate(width / 2 + x, y)
        painter.scale(scale, scale)
        rect = QRectF(0, -metrics.height() / 2, advance + 2, metrics.height())
        opacity = round(56 + 194 * emphasis)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine
        painter.setPen(QColor(255, 255, 255, round(opacity * .14)) if dark else QColor(0, 0, 0, round(opacity * .16)))
        painter.drawText(rect.translated(0, .7), flags, text)
        painter.setPen(QColor(24, 38, 50, opacity) if dark else QColor(250, 253, 255, opacity))
        painter.drawText(rect, flags, text)
        painter.restore()
    painter.setClipping(False)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    fade = QLinearGradient(0, 0, 0, height)
    for position, alpha in ((0., 0), (.06, 0), (.16, 120), (.30, 255),
                            (.70, 255), (.84, 120), (.94, 0), (1., 0)):
        fade.setColorAt(position, QColor(255, 255, 255, alpha))
    painter.fillRect(QRectF(0, 0, width, height), fade)
    painter.end()
    return image
