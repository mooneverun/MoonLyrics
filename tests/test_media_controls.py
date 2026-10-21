"""Use off-screen widgets and a fake reader; never change the user's music."""
from dataclasses import replace
import sys
import unittest
from unittest.mock import Mock, patch

if sys.platform == "win32":
    from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
    from PyQt6.QtGui import QEnterEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from moonlyrics import windows as native
    from moonlyrics.i18n import LANGUAGES, translate
    from moonlyrics.lyrics import Lyrics, LyricLine
    from moonlyrics.playback import PlaybackSnapshot
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == "win32", "Windows lyric controls")
class MediaControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        with patch.object(native.QSystemTrayIcon, "show"):
            self.control = native.Controls(dict(DEFAULTS), save=False, media=False)
        for timer in self.control.findChildren(QTimer): timer.stop()
        self.glass = self.control.glass
        self.glass.paused = True
        self.glass.move(-30000, -30000); self.glass.show(); QTest.qWait(30)
        self.control.media_reader = Mock(request_command=Mock(return_value=True))
        self.snapshot = PlaybackSnapshot(source="AppleMusic!App", title="月光", status="playing",
                                          controls=("previous", "toggle", "next"))
        self.control.set_playback(self.snapshot)

    def tearDown(self):
        for timer in self.control.findChildren(QTimer): timer.stop()
        self.control.media_reader = None
        self.control.tray.hide(); self.glass.close()
        self.glass.deleteLater(); self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def enter(self):
        self.app.sendEvent(self.glass, QEnterEvent(QPointF(20, 40), QPointF(20, 40), QPointF(-29980, -29960)))

    def test_hover_fades_controls_and_reserves_text_space_without_resizing_the_bar(self):
        geometry = self.glass.geometry()
        self.assertFalse(self.glass.transport.isVisible())
        self.enter(); QTest.qWait(75)
        self.assertTrue(self.glass.transport.isVisible())
        self.assertGreater(self.glass.transport_effect.opacity(), 0.)
        self.assertLess(self.glass.transport_effect.opacity(), 1.)
        QTest.qWait(140)
        self.assertEqual(self.glass.view.control_space, self.glass.transport.width())
        self.assertEqual(self.glass.view.lyric_width(), self.glass.transport.x())
        self.assertEqual(self.glass.geometry(), geometry)
        self.app.sendEvent(self.glass, QEvent(QEvent.Type.Leave)); QTest.qWait(210)
        self.assertFalse(self.glass.transport.isVisible())
        self.assertEqual(self.glass.view.control_space, 0)
        self.assertEqual(self.glass.geometry(), geometry)

    def test_control_region_shares_the_outer_edges_at_all_supported_sizes(self):
        self.enter(); QTest.qWait(210)
        for width, height in ((320, 80), (580, 80), (960, 240)):
            with self.subTest(size=(width, height)):
                self.glass.resize(width, height); self.app.processEvents()
                region = self.glass.transport.geometry()
                self.assertEqual(region.top(), 0)
                self.assertEqual(region.right(), self.glass.rect().right())
                self.assertEqual(region.bottom(), self.glass.rect().bottom())
                self.assertEqual(self.glass.view.lyric_width(), region.left())
                self.assertIn("background: transparent", self.glass.transport.styleSheet())
                self.assertFalse(self.glass.transport.autoFillBackground())
                self.assertEqual(self.glass.transport_divider.geometry().center().y(), (height - 1) // 2)
                for button in self.glass.media_buttons.values():
                    self.assertTrue(self.glass.transport.rect().contains(button.geometry()))
                    self.assertGreaterEqual(button.x(), 10)
                    self.assertLessEqual(button.geometry().right(), region.width() - 11)

    def test_icons_follow_the_glass_contrast_and_keep_the_actual_playback_state(self):
        for dark in (True, False):
            self.glass.view.dark_text = dark
            self.glass.view.tone_changed.emit()
            self.assertEqual(self.glass.media_dark, dark)
            for action, button in self.glass.media_buttons.items():
                name = "pause" if action == "toggle" else action
                self.assertEqual(button.icon().cacheKey(), self.glass.media_icons[name].cacheKey())
                image = button.icon().pixmap(20, 20).toImage()
                opaque = [image.pixelColor(x, y) for x in range(20) for y in range(20)
                          if image.pixelColor(x, y).alpha() > 240]
                self.assertTrue(opaque)
                self.assertTrue(all(color.lightness() < 70 if dark else color.lightness() > 220
                                    for color in opaque))
        self.control.set_playback(replace(self.snapshot, status="paused"))
        self.glass.view.dark_text = True; self.glass.view.tone_changed.emit()
        self.assertEqual(self.glass.media_buttons["toggle"].icon().cacheKey(), self.glass.media_icons["play"].cacheKey())

    def test_lock_hides_controls_and_preserves_click_through(self):
        self.enter(); QTest.qWait(210)
        self.glass.set_locked(True); QTest.qWait(30)
        self.enter(); QTest.qWait(210)
        self.assertFalse(self.glass.transport.isVisible())
        self.assertEqual(self.glass.view.control_space, 0)
        self.assertTrue(self.glass.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.glass.set_locked(False); self.enter(); QTest.qWait(210)
        self.assertTrue(self.glass.transport.isVisible())

    def test_buttons_send_once_without_drag_and_wait_for_completion(self):
        self.enter(); QTest.qWait(210)
        geometry = self.glass.geometry()
        button = self.glass.media_buttons["next"]
        self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertFalse(button.icon().isNull())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.control.media_reader.request_command.assert_called_once_with("next")
        self.assertTrue(self.control.media_command_busy)
        self.assertTrue(all(not b.isEnabled() for b in self.glass.media_buttons.values()))
        self.assertIsNone(self.glass.drag_anchor)
        self.assertEqual(self.glass.geometry(), geometry)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.control.media_reader.request_command.call_count, 1)
        self.control.media_command_finished("next", "")
        self.assertTrue(all(b.isEnabled() for b in self.glass.media_buttons.values()))
        point = button.mapTo(self.glass, QPoint(10, 10))
        button.setEnabled(False)
        QTest.mousePress(self.glass, Qt.MouseButton.LeftButton, pos=point)
        self.assertIsNone(self.glass.drag_anchor, "A disabled control started moving the lyric bar")

    def test_real_playback_feedback_and_language_change_update_icons_and_labels(self):
        button = self.glass.media_buttons["toggle"]
        for language in LANGUAGES:
            self.control.language.setCurrentIndex(self.control.language.findData(language))
            self.assertEqual(button.toolTip(), translate(language, "暂停"))
            self.assertEqual(button.icon().cacheKey(), self.glass.media_icons["pause"].cacheKey())
            self.assertEqual(self.glass.media_buttons["next"].accessibleName(), translate(language, "下一曲"))
        self.control.set_playback(replace(self.snapshot, status="paused", controls=("toggle",)))
        self.assertEqual(button.toolTip(), translate("ko", "播放"))
        self.assertEqual(button.icon().cacheKey(), self.glass.media_icons["play"].cacheKey())
        self.assertFalse(self.glass.media_buttons["next"].isEnabled())
        self.control.set_playback(PlaybackSnapshot())
        self.assertTrue(all(not b.isEnabled() for b in self.glass.media_buttons.values()))

    def test_control_failure_preserves_lyrics_and_reenables_buttons(self):
        document = Lyrics((LyricLine(0, "月光"),))
        self.glass.view.set_lyrics(document)
        self.control.send_media_command("toggle")
        with patch.object(native.QToolTip, "showText") as tooltip:
            self.control.media_command_finished("toggle", "播放控制失败，请重试")
            tooltip.assert_called_once()
        self.assertFalse(self.control.media_command_busy)
        self.assertIs(self.glass.view.lyrics, document)
        self.assertTrue(self.glass.media_buttons["toggle"].isEnabled())
        self.assertTrue(self.glass.paused)


if __name__ == "__main__":
    unittest.main()
