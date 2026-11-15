"""Readable day/night settings chrome over the shared liquid glass renderer."""
import ctypes as C
from ctypes import wintypes as W
import logging
from pathlib import Path
import sys

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPalette
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                            QSizeGrip, QVBoxLayout, QWidget)
from .settings_glass import SettingsBackdrop
from .settings_icons import settings_icon

LOG = logging.getLogger("moonlyrics")
ASSETS = Path(__file__).resolve().parents[1] / "assets"


def application_icon():
    return QIcon(str(ASSETS / "moonlyrics-icon.ico"))


def media_icon(action, dark=False):
    image = QPixmap(60, 60)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2.5, 2.5)
    path = QPainterPath()
    if action == "pause":
        path.addRoundedRect(6, 5, 4, 14, 1, 1)
        path.addRoundedRect(14, 5, 4, 14, 1, 1)
    else:
        if action == "previous":
            painter.translate(24, 0); painter.scale(-1, 1)
        if action in ("previous", "next"):
            path.addRect(20, 6, 2, 12)
            starts = (3, 11)
        else:
            starts = (8,)
        for x in starts:
            path.moveTo(x, 5 if action == "play" else 6)
            path.lineTo(x, 19 if action == "play" else 18)
            path.lineTo(x + (11 if action == "play" else 9), 12)
            path.closeSubpath()
    painter.fillPath(path, QColor("#182632" if dark else "#f1f6ff")); painter.end()
    return QIcon(image)


def native_window_theme(hwnd, night):
    """Keep native borders from adding a second material or conflicting corners."""
    if sys.platform != "win32":
        return
    try:
        dwm = C.WinDLL("dwmapi")
        attribute = dwm.DwmSetWindowAttribute
        attribute.argtypes = [W.HWND, W.DWORD, C.c_void_p, W.DWORD]
        attribute.restype = C.c_long
        for key, number in ((20, int(night)), (33, 1), (38, 1)):
            value = C.c_int(number)
            attribute(hwnd, key, C.byref(value), C.sizeof(value))

    except (OSError, AttributeError):
        LOG.debug("Settings native border theme unavailable", exc_info=True)


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setFixedHeight(80)
        row = QHBoxLayout(self)
        row.setContentsMargins(22, 20, 22, 12)
        row.setSpacing(11)
        icon = QLabel()
        icon.setPixmap(application_icon().pixmap(38, 38))
        icon.setFixedSize(38,38)
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(icon)
        heading = QVBoxLayout()
        heading.setSpacing(1)
        for text, name, height in (("MoonLyrics", "appTitle",29), ("月光歌词 · 设置", "caption",18)):
            label = QLabel(text)
            label.setObjectName(name)
            label.setFixedHeight(height)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            heading.addWidget(label)
        row.addLayout(heading)
        row.addStretch()
        actions = QHBoxLayout(); actions.setSpacing(1)
        for name, label, callback in (("minus", "最小化设置", window.minimize_settings),
                                      ("square", "最大化或还原", self.toggle_maximized),
                                      ("x", "关闭", window.close)):
            button = QPushButton()
            button.setObjectName("closeWindow" if name == "x" else "windowButton")
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.setFixedSize(32,32);button.setIconSize(QSize(16,16))
            window.chrome_icons.append((button,name,16,'secondary'))
            button.clicked.connect(callback)
            actions.addWidget(button)
        row.addLayout(actions)

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
        self.material = (237, 241, 245, 255)
        self.night = False
        self.backdrop_active = False
        self.settings_backdrop = None
        self.backdrop_dragging = False
        self.chrome_icons = []
        self.icon_state = None
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowIcon(application_icon())
        self.setMouseTracking(True)
        font=QFont();font.setFamilies(['Segoe UI','Microsoft YaHei UI']);font.setPixelSize(14);self.setFont(font)
        self.setMinimumSize(584, 520)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0,0,0,0)
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
        self.content_layout.setContentsMargins(26,8,26,0)
        self.content_layout.setSpacing(0)
        self.scroll.setWidget(content)
        content.setAutoFillBackground(False)
        shell.addWidget(self.scroll)
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(16, 16)
        self.size_grip.hide()

    def section(self, title):
        card = QFrame()
        card.setObjectName("nowPlayingCard" if title == "正在播放" else "settingsCard" if title else "settingsGeneral")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)
        if title:
            heading = QLabel(title)
            heading.setObjectName("sectionTitle")
            layout.addWidget(heading)
        self.content_layout.addWidget(card)
        return layout

    def attach_glass(self, view_factory, settings, shared_with):
        # Controls live inside the GL surface so the native translucent
        # window composes the background and its controls together.
        self.settings_backdrop = SettingsBackdrop(self, view_factory, settings, shared_with)
        self.settings_backdrop.view.setLayout(self.layout())

    def restore_plain_surface(self):
        view=self.settings_backdrop.view
        if view.layout() is not None:
            self.setLayout(view.layout())
        view.hide()
        self.title_bar.show();self.scroll.show()
        self.update()

    def apply_appearance(self, night):
        self.night = bool(night)
        self.setProperty("appearance", "night" if night else "day")
        ink, secondary, base = (("#f5f5f7", "#d6dce4", "#273340") if night
                                else ("#1d1d1f", "#30343a", "#edf1f5"))
        selected, selected_ink = ("#e9f1f8", "#1d2630") if night else ("#1d2834", "#ffffff")
        control = "rgba(255,255,255,18)" if night else "rgba(255,255,255,102)"
        hover = "rgba(255,255,255,36)" if night else "rgba(255,255,255,166)"
        line = "rgba(242,245,250,36)" if night else "rgba(37,51,69,30)"
        ring = "#bdd5ed" if night else "#466a84"
        self.material = QColor(base).getRgb()
        palette = self.palette()
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
            palette.setColor(role, QColor(ink))
        palette.setColor(QPalette.ColorRole.Base, QColor(base))
        palette.setColor(QPalette.ColorRole.Window, QColor(base))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(selected))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(selected_ink))
        self.setPalette(palette)
        self.setStyleSheet("""
            QWidget { color: INK; font-size: 14px; }
            QWidget#settingsContent, QWidget#settingsViewport, QScrollArea#settingsScroll { background: transparent; border: none; }
            QFrame#settingsGeneral { background: transparent; border: none; }
            QFrame#settingsSection { background: transparent; border: none; border-bottom: 1px solid LINE; border-radius: 0; }
            QFrame#nowPlayingCard { background: CONTROL; border: 1px solid LINE; border-radius: 20px; }
            QLabel { background: transparent; border: none; }
            QLabel#appTitle { font-size: 19px; font-weight: 650; }
            QLabel#caption, QLabel#secondary, QLabel#sliderValue { color: SECONDARY; font-size: 12px; }
            QLabel#sectionTitle, QLabel#settingsHeading { color: INK; font-size: 14px; font-weight: 650; }
            QLabel#trackTitle { font-size: 23px; font-weight: 650; }
            QLabel#trackDetails { color: SECONDARY; font-size:13px; }
            QLabel#playingEyebrow, QLabel#playingConnection, QLabel#playingTime, QLabel#matchStatus, QLabel#reserveSource, QLabel#runtimeSummary, QLabel#versionLabel { color: SECONDARY; font-size:11px; }
            QLabel#playingEyebrow { font-weight:600; }
            QLabel#statusDot { color:SECONDARY; font-size:8px; }
            QLabel#errorStatus { color: ERROR; }
            QPushButton, QComboBox { background: CONTROL; border: 1px solid LINE; border-radius: 12px; padding: 0 13px; }
            QPushButton:hover, QComboBox:hover { background: HOVER; border-color: LINE; }
            QPushButton:pressed { background: HOVER; }
            QPushButton:checked { background: SELECTED; color: SELECTED_INK; border-color: SELECTED; }
            QPushButton:focus, QComboBox:focus { border-color: RING; }
            QPushButton:disabled { color: SECONDARY; background: CONTROL; }
            QFrame#appearanceSegment { background:CONTROL; border:1px solid LINE; border-radius:15px; }
            QPushButton#appearanceChoice { padding:0 9px; border:none; border-radius:11px; font-size:13px; background:transparent; }
            QPushButton#appearanceChoice:checked { background:SELECTED; color:SELECTED_INK; }
            QPushButton#calibrationButton { font-size:12px; }
            QPushButton#positionChoice { font-size:12px; padding:0 4px; }
            QPushButton#runtimeButton { background:transparent; border:none; border-radius:0; padding:0; color:SECONDARY; font-size:11px; }
            QToolButton#settingsTransport { border: none; background: transparent; border-radius: 12px; }
            QToolButton#settingsTransport:hover { background: CONTROL; }
            QToolButton#settingsTransport:pressed { background: HOVER; }
            QToolButton#settingsPlay { border:none; background:CONTROL; border-radius:18px; }
            QToolButton#settingsPlay:hover { background:HOVER; }
            QPushButton#windowButton, QPushButton#closeWindow { padding: 0; min-height: 0; border: none; background: transparent; font-size: 18px; }
            QPushButton#windowButton:hover { background: CONTROL; }
            QPushButton#closeWindow:hover { background: HOVER; }
            QComboBox { padding-right: 22px; border-radius:11px; }
            QComboBox#sourceChoice, QComboBox#settingsFont { font-size:13px; padding-left:12px; padding-right:22px; }
            QComboBox::drop-down { border: none; width: 21px; }
            QComboBox::down-arrow { image: url("CHEVRON_PATH"); width: 12px; height: 12px; }
            QComboBox QAbstractItemView { background: BASE; color: INK; border: 1px solid LINE; selection-background-color: SELECTED; selection-color: SELECTED_INK; padding: 5px; outline: none; }
            QSlider::groove:horizontal { background: TRACK; height: 6px; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: INK; border-radius: 3px; }
            QSlider::handle:horizontal { background: INK; border: none; width: 14px; margin: -5px 0; border-radius: 7px; }
            QSlider::handle:horizontal:hover { background: SECONDARY; }
            QSlider:focus { border: 1px solid RING; border-radius: 5px; }
            QProgressBar { border: none; border-radius: 2px; background: LINE; }
            QProgressBar::chunk { background: PROGRESS; border-radius: 2px; }
            QScrollBar:vertical { background: transparent; width: 7px; margin: 4px 0; }
            QScrollBar::handle:vertical { background: LINE; border-radius: 3px; min-height: 32px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QToolTip { color: INK; background: BASE; border: 1px solid LINE; padding: 6px; }
            QMenu { color: INK; background: BASE; border: 1px solid LINE; border-radius: 8px; padding: 6px; }
            QMenu::item { padding: 7px 24px 7px 12px; border-radius: 5px; }
            QMenu::item:selected { background: SELECTED; color: SELECTED_INK; }
            QMenu::separator { height: 1px; background: LINE; margin: 4px 6px; }
            QFrame#advancedSettings { background:BASE; border:none; }
        """.replace("SELECTED_INK", selected_ink).replace("SELECTED", selected)
               .replace("SECONDARY", secondary).replace("INK", ink).replace("CONTROL", control)
               .replace("HOVER", hover).replace("LINE", line).replace("RING", ring)
               .replace("BASE", base).replace("TRACK", "rgba(245,245,247,28)" if night else "#e5e7eb")
               .replace("PROGRESS", "rgba(245,245,247,189)" if night else "rgba(29,29,31,189)")
               .replace("ERROR", "#ffb8bc" if night else "#a32637")
               .replace("CHEVRON_PATH", (ASSETS / ("settings-chevron.svg" if night else "settings-chevron-dark.svg")).as_posix()))
        if self.isVisible():
            native_window_theme(int(self.winId()), night)
        self.update_ui_icons(force=True)
        self.update()

    def update_ui_icons(self, force=False):
        nodes=getattr(self,'ui_icons',self.chrome_icons)
        state=(self.night,self.devicePixelRatioF(),tuple(item.isChecked() for item,_,_,role in nodes if role in ('choice','position')))
        if not force and state==self.icon_state:return
        self.icon_state=state
        for item,name,size,role in nodes:
            selected=role in ('choice','position') and item.isChecked()
            color=('#1d2630' if self.night else '#ffffff') if selected else (
                ('#d6dce4' if self.night else '#30343a') if role=='secondary' else ('#f5f5f7' if self.night else '#1d1d1f'))
            icon=settings_icon(name,color,size,self.devicePixelRatioF())
            if isinstance(item,QLabel):item.setPixmap(icon.pixmap(size,size))
            else:item.setIcon(icon)

    def minimize_settings(self):
        self.showMinimized()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.update_backdrop)

    def update_backdrop(self):
        native_window_theme(int(self.winId()), self.night)
        self.update()

    def paintEvent(self, event):
        if self.backdrop_active:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(*self.material)
        color.setAlpha(255)
        painter.setBrush(color)
        painter.setPen(QPen(QColor(0,0,0,20) if not self.night else QColor(255,255,255,32), 1))
        rect = self.rect().toRectF().adjusted(.5, .5, -.5, -.5)
        if self.isMaximized():
            painter.drawRect(rect)
        else:
            painter.drawRoundedRect(rect, 30, 30)

    def closeEvent(self, event):
        if self.settings_backdrop:
            self.settings_backdrop.shutdown()
        super().closeEvent(event)

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
