"""Exercise Windows Z-order recovery using only off-screen test windows."""
import ctypes as C
from ctypes import wintypes as W
import sys
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    from PyQt6.QtCore import QEvent, Qt, QTimer
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget
    from moonlyrics import windows as native
    from moonlyrics.playback import PlaybackSnapshot
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == "win32", "Windows window-manager integration")
class WindowTopmostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.position = native.api(native.user32, "SetWindowPos", W.BOOL,
                                  W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
        cls.style = native.api(native.user32, "GetWindowLongPtrW", C.c_ssize_t, W.HWND, C.c_int)
        cls.previous = native.api(native.user32, "GetWindow", W.HWND, W.HWND, W.UINT)
        cls.foreground = native.api(native.user32, "GetForegroundWindow", W.HWND)

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
        self.assertTrue(self.topmost())

    def tearDown(self):
        for timer in self.control.findChildren(QTimer):
            timer.stop()
        self.control.tray.hide()
        self.glass.close()
        self.glass.deleteLater()
        self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def topmost(self):
        return bool(self.style(int(self.glass.winId()), -20) & 0x8)

    def demote(self):
        self.assertTrue(self.position(int(self.glass.winId()), 1, 0, 0, 0, 0, 0x13))
        self.assertFalse(self.topmost())

    def peer_above(self, peer):
        current = int(peer.winId())
        while current:
            current = self.previous(current, 3)
            if current == int(self.glass.winId()):
                return False
        return True

    def playing(self, status):
        self.control.set_playback(PlaybackSnapshot(title="Window test", artist="MoonLyrics",
                                                   status=status, position=30, duration=180))

    def test_resume_reasserts_topmost_without_focus_or_geometry_changes(self):
        self.playing("paused")
        self.demote()
        geometry = self.glass.geometry()
        self.playing("playing")
        self.assertTrue(self.topmost(), "Resuming playback left the lyric window in the ordinary window band")
        self.assertEqual(self.glass.geometry(), geometry)
        self.assertNotEqual(self.foreground(), int(self.glass.winId()))

    def test_resume_raises_within_the_existing_topmost_band(self):
        peer = QWidget()
        self.addCleanup(peer.deleteLater)
        peer.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.WindowDoesNotAcceptFocus)
        peer.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        peer.setGeometry(-30000, -30000, 100, 50)
        peer.show()
        self.addCleanup(peer.close)
        self.assertTrue(self.position(int(peer.winId()), -1, 0, 0, 0, 0, 0x13))
        self.assertTrue(self.topmost())
        self.assertTrue(self.peer_above(peer))
        with patch.object(native, "set_window_pos", wraps=native.set_window_pos) as position:
            self.control.sample()
            position.assert_not_called()
        self.assertTrue(self.peer_above(peer), "The status tick must not fight other topmost windows")
        self.playing("paused")
        self.playing("playing")
        self.assertFalse(self.peer_above(peer), "Resume did not restore lyric Z-order within the topmost band")

    def test_existing_status_tick_recovers_while_settings_are_hidden(self):
        self.assertFalse(self.control.isVisible())
        self.demote()
        self.control.sample()
        self.assertTrue(self.topmost(), "The normal status tick did not recover lost topmost state")

    def test_idle_tick_recovers_stale_topmost_flag_above_ordinary_windows(self):
        self.playing("paused")
        peer = QWidget()
        self.addCleanup(peer.deleteLater)
        self.addCleanup(peer.close)
        peer.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowDoesNotAcceptFocus)
        peer.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        peer.setGeometry(-30000, -30000, 100, 50)
        self.demote()
        peer.show()
        self.assertTrue(self.position(int(peer.winId()), 0, 0, 0, 0, 0, 0x13))
        hwnd = int(self.glass.winId())
        self.assertTrue(self.peer_above(peer))
        foreground, geometry = self.foreground(), self.glass.geometry()
        self.assertFalse(self.control.isVisible())
        read_style = native.get_window_long
        def stale_flag(window, index):
            value = read_style(window, index)
            return value | 0x8 if window == hwnd and index == -20 else value
        # Windows cannot safely be forced into the live inconsistency; inject only its stale flag.
        with patch.object(native, "get_window_long", side_effect=stale_flag):
            self.assertTrue(native.get_window_long(hwnd, -20) & 0x8)
            self.control.sample()
        self.assertFalse(self.peer_above(peer), "The idle tick trusted a stale topmost flag")
        self.assertTrue(self.topmost())
        self.assertEqual(self.foreground(), foreground)
        self.assertEqual(self.glass.geometry(), geometry)
        self.assertTrue(self.glass.paused, "Z-order recovery must not resume paused rendering")

    def test_lock_and_show_cycle_preserve_capture_permission_and_input_mode(self):
        geometry = self.glass.geometry()
        for locked in (True, False):
            self.glass.set_locked(locked)
            QTest.qWait(30)
            hwnd = int(self.glass.winId())
            self.assertTrue(self.topmost())
            self.assertEqual(bool(self.style(hwnd, -20) & 0x20), locked)
            affinity = W.DWORD()
            self.assertTrue(native.get_affinity(hwnd, C.byref(affinity)))
            self.assertEqual(affinity.value, 0)
            self.assertEqual(self.glass.geometry(), geometry)

    def test_status_tick_does_not_show_a_hidden_window(self):
        self.glass.hide()
        self.control.sample()
        self.assertFalse(self.glass.isVisible())


if __name__ == "__main__":
    unittest.main()
