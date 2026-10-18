"""A clipped, vertically faded lyric layer; the glass itself is left untouched."""
import math

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath


def lyric_layer(document, scroll, width, height, dpr, dark):
    image = QImage(math.ceil(width * dpr), math.ceil(height * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr); image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    clip = QPainterPath(); clip.addRoundedRect(QRectF(0, 0, width, height), min(24., height / 2), min(24., height / 2))
    painter.setClipPath(clip)
    spacing = max(29., min(46., height * .36))
    font = QFont("Microsoft YaHei UI", 20)
    font.setWeight(QFont.Weight.DemiBold)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    painter.setFont(font)
    metrics = QFontMetricsF(font)
    start, end = math.floor(scroll) - 4, math.ceil(scroll) + 5
    for index in range(max(-1, start), min(len(document.lines), end)):
        y = height / 2 + (index - scroll) * spacing
        if y < -20 or y > height + 20:
            continue
        text = document.lines[index].text if index >= 0 else "♪"
        text = text or "♪"
        emphasis = max(0., 1 - abs(index - scroll))
        emphasis = emphasis * emphasis * (3 - 2 * emphasis)
        fit = max(.6, min(1., (width - 36) / max(1., metrics.horizontalAdvance(text))))
        text_width = (width - 32) / fit
        text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_width)
        # Scale fixed glyphs continuously; changing font weight used to pop mid-transition.
        painter.save(); painter.translate(width / 2, y)
        scale = fit * (.72 + .28 * emphasis)
        painter.scale(scale, scale)
        rect = QRectF(-text_width / 2, -metrics.height() / 2, text_width, metrics.height())
        opacity = round(76 + 179 * emphasis)
        painter.setPen(QColor(255, 255, 255, round(opacity * .35)) if dark else QColor(0, 0, 0, round(opacity * .5)))
        painter.drawText(rect.translated(0, 1), Qt.AlignmentFlag.AlignCenter, text)
        painter.setPen(QColor(24, 38, 50, opacity) if dark else QColor(250, 253, 255, opacity))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
    painter.setClipping(False)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    fade = QLinearGradient(0, 0, 0, height)
    for position, alpha in ((0., 0), (.23, 255), (.77, 255), (1., 0)):
        fade.setColorAt(position, QColor(255, 255, 255, alpha))
    painter.fillRect(QRectF(0, 0, width, height), fade)
    painter.end()
    return image
