"""Qt settings shell; native backdrop stays separate from the lyric renderer."""
import ctypes as C
from ctypes import wintypes as W
import logging
from pathlib import Path
import sys

from PyQt6.QtCore import QEasingCurve, Qt, QTimer, QVariantAnimation
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPalette
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                            QSizeGrip, QVBoxLayout, QWidget)

LOG = logging.getLogger("moonlyrics")
ASSETS = Path(__file__).resolve().parents[1] / "assets"
MATERIALS = {"rich": (32, 44, 63, 230)}


def application_icon():
    return QIcon(str(ASSETS / "moonlyrics-icon.ico"))


def native_backdrop(hwnd):
    """Use documented Desktop Acrylic on Windows 11; older systems get a solid tint."""
    if sys.platform != "win32" or sys.getwindowsversion().build < 22621:
        return False
    try:
        dwm = C.WinDLL("dwmapi")
        attribute = dwm.DwmSetWindowAttribute
        attribute.argtypes = [W.HWND, W.DWORD, C.c_void_p, W.DWORD]
        attribute.restype = C.c_long
        for key, number in ((20, 1), (33, 2)):
            value = C.c_int(number)
            attribute(hwnd, key, C.byref(value), C.sizeof(value))

        class Margins(C.Structure):
            _fields_ = [(name, C.c_int) for name in ("left", "right", "top", "bottom")]

        extend = dwm.DwmExtendFrameIntoClientArea
        extend.argtypes = [W.HWND, C.POINTER(Margins)]
        extend.restype = C.c_long
        if extend(hwnd, C.byref(Margins(-1, -1, -1, -1))) < 0:
            return False
        backdrop = C.c_int(3)  # DWMSBT_TRANSIENTWINDOW / Desktop Acrylic.
        return attribute(hwnd, 38, C.byref(backdrop), C.sizeof(backdrop)) >= 0
    except (OSError, AttributeError):
        LOG.exception("Settings system backdrop unavailable")
        return False


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setFixedHeight(62)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 6, 10, 6)
        row.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(application_icon().pixmap(40, 40))
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(icon)
        heading = QVBoxLayout()
        heading.setSpacing(0)
        for text, name in (("月光歌词", "appTitle"), ("MoonLyrics · 设置", "caption")):
            label = QLabel(text)
            label.setObjectName(name)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            heading.addWidget(label)
        row.addLayout(heading)
        row.addStretch()
        for text, label, callback in (("−", "收起到托盘", window.minimize_settings),
                                      ("□", "最大化或还原", self.toggle_maximized),
                                      ("×", "关闭", window.close)):
            button = QPushButton(text)
            button.setObjectName("closeWindow" if text == "×" else "windowButton")
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.setFixedSize(32, 30)
            button.clicked.connect(callback)
            row.addWidget(button)

    def toggle_maximized(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()


class SettingsWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.material = MATERIALS["rich"]
        self.backdrop_active = False
        self.transparency_level = 1.
        self.transparency_animation = QVariantAnimation(self)
        self.transparency_animation.setDuration(240)
        self.transparency_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.transparency_animation.valueChanged.connect(self.animate_transparency)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowIcon(application_icon())
        self.setMouseTracking(True)
        self.setMinimumSize(584, 520)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(8, 5, 8, 4)
        shell.setSpacing(0)
        self.title_bar = TitleBar(self)
        shell.addWidget(self.title_bar)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("settingsScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.viewport().setObjectName("settingsViewport")
        content = QWidget()
        content.setObjectName("settingsContent")
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(16, 8, 16, 10)
        self.content_layout.setSpacing(14)
        self.scroll.setWidget(content)
        shell.addWidget(self.scroll)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(18, 4, 2, 0)
        caption = QLabel("APPLE MUSIC  /  桌面歌词")
        caption.setObjectName("caption")
        bottom.addWidget(caption)
        bottom.addStretch()
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(16, 16)
        bottom.addWidget(self.size_grip)
        shell.addLayout(bottom)

    def section(self, title):
        card = QFrame()
        card.setObjectName("settingsCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 17)
        layout.setSpacing(12)
        if title:
            heading = QLabel(title)
            heading.setObjectName("sectionTitle")
            layout.addWidget(heading)
        self.content_layout.addWidget(card)
        return layout

    def apply_material(self, preset):
        self.material = MATERIALS[preset]
        self.setProperty("glassPreset", preset)
        palette = self.palette()
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
            palette.setColor(role, QColor("#e7effa"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#26354b"))
        palette.setColor(QPalette.ColorRole.Window, QColor("#233047"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#bddaff"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#192b43"))
        self.setPalette(palette)
        # Cards retain contrast even when the system reduces backdrop transparency.
        self.setStyleSheet("""
            QWidget { color: #e7effa; }
            QWidget#settingsContent, QWidget#settingsViewport, QScrollArea#settingsScroll { background: transparent; border: none; }
            QFrame#settingsCard { background: rgba(255,255,255,21); border: 1px solid rgba(222,237,255,24); border-radius: 16px; }
            QLabel { background: transparent; border: none; }
            QLabel#appTitle { font-size: 19px; font-weight: 600; }
            QLabel#caption, QLabel#secondary { color: #a9bad0; font-size: 11px; }
            QLabel#sectionTitle { color: #cadbf1; font-size: 12px; font-weight: 600; }
            QLabel#trackTitle { font-size: 19px; font-weight: 600; }
            QLabel#trackDetails { color: #a9bad0; }
            QLabel#errorStatus { color: #ffb8bc; }
            QPushButton, QComboBox { background: rgba(235,245,255,14); border: 1px solid rgba(224,238,255,32); border-radius: 8px; padding: 5px 10px; min-height: 20px; }
            QPushButton:hover, QComboBox:hover { background: rgba(224,240,255,28); border-color: rgba(218,235,255,80); }
            QPushButton:pressed { background: rgba(191,218,255,55); }
            QPushButton:focus, QComboBox:focus { border-color: #bcd9ff; }
            QPushButton:disabled { color: #8c9bae; background: rgba(255,255,255,5); }
            QPushButton#windowButton, QPushButton#closeWindow { padding: 0; min-height: 0; border: none; background: transparent; font-size: 18px; }
            QPushButton#windowButton:hover { background: rgba(255,255,255,20); }
            QPushButton#closeWindow:hover { background: #a84655; }
            QComboBox { padding-right: 22px; }
            QComboBox::drop-down { border: none; width: 21px; }
            QComboBox::down-arrow { image: url("CHEVRON_PATH"); width: 12px; height: 12px; }
            QComboBox QAbstractItemView { background: #293950; color: #e7effa; border: 1px solid #65768e; selection-background-color: #405b7c; selection-color: white; padding: 5px; outline: none; }
            QSlider::groove:horizontal { background: rgba(229,240,255,28); height: 4px; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #aacdf5; border-radius: 2px; }
            QSlider::handle:horizontal { background: #e2efff; border: 1px solid #a2bddf; width: 14px; margin: -6px 0; border-radius: 7px; }
            QSlider::handle:horizontal:hover { background: white; }
            QSlider:focus { border: 1px solid rgba(190,218,255,120); border-radius: 5px; }
            QProgressBar { border: none; border-radius: 2px; background: rgba(230,242,255,22); }
            QProgressBar::chunk { background: #b7d8ff; border-radius: 2px; }
            QCheckBox { spacing: 9px; background: transparent; }
            QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #8397b1; border-radius: 5px; background: rgba(255,255,255,12); }
            QCheckBox::indicator:checked { background: #bddaff; border-color: #d4e7ff; image: url("CHECK_PATH"); }
            QScrollBar:vertical { background: transparent; width: 7px; margin: 4px 0; }
            QScrollBar::handle:vertical { background: rgba(215,233,255,65); border-radius: 3px; min-height: 32px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QToolTip { color: #eaf2ff; background: #293950; border: 1px solid #65768e; padding: 6px; }
            QMenu { color: #e7effa; background: #293950; border: 1px solid #65768e; border-radius: 8px; padding: 6px; }
            QMenu::item { padding: 7px 24px 7px 12px; border-radius: 5px; }
            QMenu::item:selected { background: #405b7c; }
            QMenu::separator { height: 1px; background: #52627a; margin: 4px 6px; }
        """.replace("CHEVRON_PATH", (ASSETS / "settings-chevron.svg").as_posix())
               .replace("CHECK_PATH", (ASSETS / "settings-check.svg").as_posix()))
        self.update()

    def set_transparency(self, level):
        self.transparency_animation.stop()
        self.transparency_animation.setStartValue(self.transparency_level)
        self.transparency_animation.setEndValue(float(max(1, min(10, level))))
        self.transparency_animation.start()

    def animate_transparency(self, level):
        self.transparency_level = float(level)
        self.update()

    def minimize_settings(self):
        self.showMinimized()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.update_backdrop)

    def update_backdrop(self):
        self.backdrop_active = native_backdrop(int(self.winId()))
        LOG.info("Settings Desktop Acrylic active=%s", self.backdrop_active)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(*self.material)
        if self.backdrop_active:
            color.setAlpha(round(self.material[3] * (1 - .85 * (self.transparency_level - 1) / 9)))
        else:
            color.setAlpha(255)
        painter.setBrush(color)
        painter.setPen(QPen(QColor(216,234,255,52), 1))
        rect = self.rect().toRectF().adjusted(.5, .5, -.5, -.5)
        # DWM clips both surfaces at one native corner. A second Qt round cutout
        # exposes the untinted Acrylic as grey wedges between the two radii.
        if self.backdrop_active or self.isMaximized():
            painter.drawRect(rect)
        else:
            painter.drawRoundedRect(rect, 18, 18)

    def mousePressEvent(self, event):
        edges = Qt.Edge(0)
        point = event.position()
        if point.x() < 8: edges |= Qt.Edge.LeftEdge
        if point.x() > self.width() - 8: edges |= Qt.Edge.RightEdge
        if point.y() < 8: edges |= Qt.Edge.TopEdge
        if point.y() > self.height() - 8: edges |= Qt.Edge.BottomEdge
        if event.button() == Qt.MouseButton.LeftButton and edges and not self.isMaximized():
            self.windowHandle().startSystemResize(edges)
        else:
            super().mousePressEvent(event)
