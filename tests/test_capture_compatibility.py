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
    from PyQt6.QtWidgets import QApplication, QWidget
    from PIL import ImageGrab
    from moonlyrics import windows as native
    from moonlyrics import windows_capture as capture
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == "win32", "Windows capture compatibility")
class CaptureCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        with patch.object(native.QSystemTrayIcon, "show"):
            self.control = native.Controls(dict(DEFAULTS), save=False, media=False)
        for timer in self.control.findChildren(QTimer):
            timer.stop()
        self.glass = self.control.glass
        self.glass.paused = True
        self.glass.move(-30000, -30000)
        self.glass.show()
        QTest.qWait(30)

    def tearDown(self):
        self.control.tray.hide()
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

    def test_capture_affinity_stays_unrestricted_after_lock_and_show_cycles(self):
        for locked in (True, False):
            self.glass.set_locked(locked)
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


if __name__ == "__main__":
    unittest.main()
