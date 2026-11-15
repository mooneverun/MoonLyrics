"""Settings hide completely and are restored by right-clicking the lyric bar."""
import sys
import unittest

if sys.platform == 'win32':
    from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QCheckBox, QSystemTrayIcon
    from moonlyrics import windows as native
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == 'win32', 'Windows settings recovery')
class SettingsRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.control = native.Controls(dict(DEFAULTS), save=False, media=False)
        for timer in self.control.findChildren(QTimer): timer.stop()
        self.glass = self.control.glass
        self.glass.paused = True
        self.glass.move(-30000, -30000); self.glass.show()
        self.control.move(-30000, -30000); self.control.show()
        QTest.qWait(30)

    def tearDown(self):
        for timer in self.control.findChildren(QTimer): timer.stop()
        self.glass.close()
        self.glass.deleteLater(); self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_minimize_hides_settings_and_leaves_only_the_lyric_window(self):
        self.control.minimize_settings(); QTest.qWait(40)
        visible = native.api(native.user32, 'IsWindowVisible', native.W.BOOL, native.W.HWND)
        self.assertFalse(visible(int(self.control.winId())))
        self.assertFalse(self.control.isVisible())
        self.assertTrue(self.glass.isVisible())
        style = native.get_window_long(int(self.glass.winId()), -20)
        self.assertTrue(style & 0x08000000)
        self.assertFalse(style & 0x00040000)
        self.assertEqual(self.control.findChildren(QSystemTrayIcon), [])

    def test_native_minimize_also_hides_settings(self):
        self.control.showMinimized(); QTest.qWait(40)
        self.assertFalse(self.control.isVisible())
        self.assertTrue(self.glass.isVisible())

    def test_right_click_restores_settings_without_dragging(self):
        self.control.minimize_settings()
        original = self.glass.pos()
        QTest.mouseClick(self.glass, Qt.MouseButton.RightButton, pos=QPoint(80, 40))
        QTest.qWait(30)
        self.assertTrue(self.control.isVisible())
        self.assertFalse(self.control.isMinimized())
        self.assertIsNone(self.glass.drag_anchor)
        self.assertEqual(self.glass.pos(), original)

    def test_right_clicking_a_transport_button_also_opens_settings(self):
        self.glass.reveal_transport(True); QTest.qWait(210)
        self.control.minimize_settings()
        QTest.mouseClick(self.glass.media_buttons['next'], Qt.MouseButton.RightButton)
        QTest.qWait(20)
        self.assertTrue(self.control.isVisible())

    def test_a_queued_minimize_cannot_hide_a_window_that_was_already_restored(self):
        self.control.showMinimized()
        self.control.restore_settings()
        QTest.qWait(30)
        self.assertTrue(self.control.isVisible())
        self.assertFalse(self.control.isMinimized())

    def test_removed_features_leave_no_lock_control_or_recovery_shortcut(self):
        self.assertEqual(self.control.findChildren(QCheckBox), [])
        for name in ('tray', 'tray_timer', 'lock', 'hotkey_filter', 'recovery_hotkey_registered'):
            self.assertFalse(hasattr(self.control, name), name)
        self.assertFalse(hasattr(self.glass, 'set_locked'))
        self.assertFalse(self.glass.windowFlags() & Qt.WindowType.WindowTransparentForInput)
