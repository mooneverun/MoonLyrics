"""The approved mockup's Lucide outlines, rendered locally at the current DPI.

See assets/Lucide-LICENSE.txt for the ISC and bundled Feather attribution.
"""
from functools import lru_cache
import json
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication

_ICONS = json.loads((Path(__file__).resolve().parents[1]/'assets/settings-icons.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=128)
def settings_icon(name, color, size=16, dpr=None):
    dpr = dpr or (QApplication.primaryScreen().devicePixelRatio() if QApplication.primaryScreen() else 1.)
    svg = Element('svg', xmlns='http://www.w3.org/2000/svg', width='24', height='24',
                  viewBox='0 0 24 24', fill='none', stroke=color,
                  **{'stroke-width':'2','stroke-linecap':'round','stroke-linejoin':'round'})
    for tag, attributes in _ICONS[name]:
        SubElement(svg, tag, attributes)
    image = QPixmap(round(size*dpr), round(size*dpr))
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(tostring(svg)).render(painter)
    painter.end()
    image.setDevicePixelRatio(dpr)
    return QIcon(image)
