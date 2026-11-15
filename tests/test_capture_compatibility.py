"""The lyric window must remain visible to other screen-capture programs."""
import ctypes as C
from ctypes import wintypes as W
import sys
import time
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    from PyQt6.QtCore import QEvent, QTimer, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtGui import QColor, QLinearGradient, QPainter
    from PyQt6.QtWidgets import QApplication, QWidget
    from PIL import ImageGrab
    from moonlyrics import windows as native
    from moonlyrics import windows_capture as capture
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == "win32", "Windows capture compatibility")
class CaptureCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Match native.main before the first QApplication/GL backing store exists.
        native.user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
        fmt=native.QSurfaceFormat();fmt.setVersion(3,3)
        fmt.setProfile(native.QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setAlphaBufferSize(8);fmt.setDepthBufferSize(0);fmt.setStencilBufferSize(0)
        fmt.setSamples(0);fmt.setSwapInterval(0);native.QSurfaceFormat.setDefaultFormat(fmt)
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.control = native.Controls(dict(DEFAULTS), save=False, media=False)
        for timer in self.control.findChildren(QTimer):
            timer.stop()
        self.glass = self.control.glass
        self.glass.paused = True
        self.glass.move(-30000, -30000)
        self.glass.show()
        QTest.qWait(30)

    def tearDown(self):
        self.glass.close()
        self.glass.deleteLater()
        self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_lyric_window_is_an_unowned_normal_window_with_a_capture_title(self):
        hwnd = int(self.glass.winId())
        style = native.get_window_long(hwnd, -20)
        self.assertFalse(style & 0x80, "Tool windows are filtered from common capture selectors")
        owner = native.api(native.user32, "GetWindow", W.HWND, W.HWND, W.UINT)(hwnd, 4)
        self.assertFalse(owner)
        self.assertEqual(self.glass.windowTitle(), "MoonLyrics · 月光歌词")

    def test_capture_affinity_stays_unrestricted_after_hide_and_show_cycles(self):
        for _ in range(2):
            self.glass.hide(); self.glass.show()
            QTest.qWait(30)
            affinity = W.DWORD()
            self.assertTrue(native.get_affinity(int(self.glass.winId()), C.byref(affinity)))
            self.assertEqual(affinity.value, 0, "The lyric window is hidden from screenshots and streaming")

    def test_private_sampling_excludes_the_overlay_but_normal_screenshots_include_it(self):
        back, overlay = QWidget(), QWidget()
        sampler = capture.DesktopRegion()
        try:
            for widget in (back, overlay):
                widget.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
                widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            bounds = self.app.primaryScreen().availableGeometry()
            back.setGeometry(bounds.left() + 60, bounds.top() + 60, 600, 320)
            overlay.setGeometry(back.x() + 100, back.y() + 100, 320, 80)
            back.setStyleSheet("background:#297ebb")
            overlay.setStyleSheet("background:#ff0000")
            back.show(); overlay.show(); QTest.qWait(100)
            hwnd = int(overlay.winId())
            def sampled(expected):
                deadline = time.perf_counter() + 3
                while time.perf_counter() < deadline:
                    frame = sampler.capture(hwnd, 0)
                    if frame is not None:
                        w, h = sampler.size
                        pixel = C.string_at(sampler.bits.value + ((h // 2) * w + w // 2) * 4, 3)
                        if tuple(reversed(pixel)) == expected:
                            return
                    QTest.qWait(15)
                self.fail("Private sampling contains the overlay or has stopped updating")
            sampled((41, 126, 187))
            origin, rect = W.POINT(), W.RECT()
            self.assertTrue(capture.client_origin(hwnd, C.byref(origin)))
            self.assertTrue(capture.client_rect(hwnd, C.byref(rect)))
            screenshot = ImageGrab.grab(bbox=(origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom), all_screens=True)
            self.assertEqual(screenshot.getpixel((rect.right // 2, rect.bottom // 2)), (255, 0, 0))
            back.setStyleSheet("background:#1fd08f"); back.update()
            sampled((31, 208, 143))
            previous = sampler.worker
            self.assertTrue(sampler.close())
            self.assertFalse(previous.is_alive())
            self.assertIsNone(sampler.worker)
            self.assertFalse(sampler.bits.value)
            for _ in range(3):
                sampled((31, 208, 143))
                previous = sampler.worker
                sampler.reset()
                self.assertFalse(previous.is_alive())
                self.assertIsNone(sampler.worker)
        finally:
            sampler.close(); overlay.close(); back.close()
            overlay.deleteLater(); back.deleteLater()

    def test_native_moving_capture_tracks_the_current_position_before_drag_ends(self):
        class Backdrop(QWidget):
            def paintEvent(self, event):
                painter = QPainter(self)
                gradient = QLinearGradient(0, 0, self.width(), 0)
                gradient.setColorAt(0, QColor(30, 70, 140))
                gradient.setColorAt(1, QColor(220, 230, 250))
                painter.fillRect(self.rect(), gradient)
        back, overlay = Backdrop(), QWidget()
        sampler = capture.DesktopRegion()
        try:
            for widget in (back, overlay):
                widget.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
                widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            bounds = self.app.primaryScreen().availableGeometry()
            back.setGeometry(bounds.left()+40, bounds.top()+50, min(1200, bounds.width()-80), 300)
            overlay.setGeometry(back.x()+20, back.y()+100, 320, 80)
            overlay.setStyleSheet("background:#ff0000")
            back.show(); overlay.show(); QTest.qWait(100)
            expected = back.grab().toImage()
            margin = round(256*overlay.devicePixelRatioF())
            hwnd = int(overlay.winId())
            def pixels(frame):
                origin, back_origin = W.POINT(), W.POINT()
                self.assertTrue(capture.client_origin(hwnd, C.byref(origin)))
                self.assertTrue(capture.client_origin(int(back.winId()), C.byref(back_origin)))
                w, h = sampler.size
                fw, fh, _ = frame
                ox, oy = sampler.offset
                actual = tuple(reversed(C.string_at(sampler.bits.value+((oy+fh//2)*w+ox+fw//2)*4, 3)))
                reference = expected.pixelColor(origin.x-back_origin.x+fw//2, origin.y-back_origin.y+fh//2).getRgb()[:3]
                return actual, reference
            deadline = time.perf_counter()+3
            while True:
                frame = sampler.capture(hwnd, 0, margin=margin)
                if frame is not None:
                    actual, reference = pixels(frame)
                    if max(abs(a-b) for a,b in zip(actual, reference)) <= 3:
                        break
                self.assertLess(time.perf_counter(), deadline)
                QTest.qWait(10)
            frames, serials = 0, set()
            step = max(1, min(8, (back.width()-360)//60))
            for index in range(60):
                overlay.move(back.x()+20+index*step, back.y()+100)
                frame = sampler.capture(hwnd, 0, margin=margin)
                if frame is not None:
                    actual, reference = pixels(frame)
                    self.assertLessEqual(max(abs(a-b) for a,b in zip(actual, reference)), 3,
                                         (index, actual, reference, frame, sampler.offset, sampler.source_rect))
                    frames += 1; serials.add(sampler.serial)
                QTest.qWait(8)
            self.assertGreaterEqual(frames, 45, "Background frames remained unavailable throughout the drag")
            self.assertGreaterEqual(len(serials), 3, "Overscan hid a stalled background sampler")
            self.assertTrue(sampler.close(), "The moving sampler did not release its native worker")
        finally:
            sampler.close(); overlay.close(); back.close()
            overlay.deleteLater(); back.deleteLater()


if __name__ == "__main__":
    unittest.main()
