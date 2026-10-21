"""Verify live language changes and the settings controls without accessing playback services."""
import json
from pathlib import Path
from string import Formatter
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from moonlyrics import __version__
from moonlyrics.i18n import LANGUAGES, TEXTS, translate, translate_message
from moonlyrics.styles import DEFAULTS, normalized

if sys.platform == "win32":
    from PyQt6.QtCore import QEvent, QRect, Qt, QTimer
    from PyQt6.QtGui import QFont, QFontDatabase
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QStyle, QStyleOptionComboBox
    from moonlyrics import windows as native
    from moonlyrics.lyrics import Lyrics, LyricLine
    from moonlyrics.playback import PlaybackSnapshot


class SettingsTextTests(unittest.TestCase):
    def test_every_translation_preserves_format_fields(self):
        def fields(text):
            return {name for _, name, _, _ in Formatter().parse(text) if name is not None}
        for source, translations in TEXTS.items():
            self.assertEqual(len(translations), 3, source)
            for text in translations:
                self.assertTrue(text.strip(), source)
                self.assertEqual(fields(source), fields(text), source)
            self.assertEqual(fields(source), fields(translate("zh_TW", source)), source)

    def test_language_width_and_fps_preferences_round_trip_and_migrate(self):
        for language in LANGUAGES:
            settings = normalized(dict(language=language, width=583, fps=120))
            self.assertEqual((settings["language"], settings["width"], settings["fps"]), (language, 580, 120))
            self.assertEqual(normalized(json.loads(json.dumps(settings))), settings)
        for invalid in (None, [], "unknown"):
            self.assertEqual(normalized(dict(language=invalid))["language"], "zh_CN")
        for width, expected in ((320, 320), (325, 330), (959, 960), (10000, 960), (-10, 320)):
            self.assertEqual(normalized(dict(width=width))["width"], expected)

    def test_worker_retry_cache_and_source_messages_are_translated(self):
        message = "未找到可靠的同步歌词；6 秒后自动重试（2/2）"
        self.assertEqual(translate_message("en", message), "No reliable timed lyrics found; retrying in 6s (2/2)")
        self.assertEqual(translate_message("en", "已匹配 · 网易云音乐 · 71 行（缓存）"),
                         "Matched · NetEase Music · 71 lines (cached)")
        self.assertIn("2회", translate_message("ko", "未找到可靠的同步歌词（已重试 2 次）"))
        self.assertIn("歌詞", translate_message("zh_TW", "未找到可靠的同步歌词"))

    def test_version_uses_the_repository_version_file(self):
        self.assertEqual(__version__, (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip())


@unittest.skipUnless(sys.platform == "win32", "Windows settings integration")
class SettingsControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        with patch.object(native.QSystemTrayIcon, "show"):
            self.control = native.Controls(dict(DEFAULTS), save=False, media=False)
        for timer in self.control.findChildren(QTimer):
            timer.stop()
        self.control.glass.paused = True

    def tearDown(self):
        for timer in self.control.findChildren(QTimer):
            timer.stop()
        self.control.tray.hide()
        self.control.glass.close()
        self.control.glass.deleteLater()
        self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def language(self, code):
        self.control.language.setCurrentIndex(self.control.language.findData(code))

    def test_font_switch_updates_lyrics_without_searching_or_changing_ui_font(self):
        document = Lyrics((LyricLine(1, "Moonlight 月光 달빛"), LyricLine(8, "下一句")))
        self.control.set_playback(PlaybackSnapshot(status="paused", position=1, duration=20))
        self.control.glass.view.set_lyrics(document, 2.)
        self.control.glass.view.lyric_image_key = object()
        ui_font = self.control.font().family()
        family = next(f for f in QFontDatabase.families() if f != DEFAULTS["lyric_font"])
        with patch.object(self.control, "request_lyrics") as search, \
                patch.object(self.control.glass.view, "update") as repaint:
            self.control.font_choice.setCurrentFont(QFont(family))
            self.assertEqual(self.control.settings["lyric_font"], family)
            self.assertIsNone(self.control.glass.view.lyric_image_key)
            repaint.assert_called()
            for language in LANGUAGES:
                self.language(language)
                self.assertEqual(self.control.font_choice.accessibleName(), translate(language, "歌词字体"))
                self.assertEqual(self.control.font_choice.currentFont().family(), family)
            search.assert_not_called()
        self.assertIs(self.control.glass.view.lyrics, document)
        self.assertEqual(self.control.glass.view.lyric_time, 2.)
        self.assertEqual(self.control.font().family(), ui_font)
        restored = normalized(json.loads(json.dumps(self.control.settings)))
        self.assertEqual(restored["lyric_font"], family)
        self.assertTrue(self.control.glass.paused)

    def test_short_lines_reuse_the_image_and_long_lines_follow_the_playback_time(self):
        view = self.control.glass.view
        view.set_lyrics(Lyrics((LyricLine(1, "月光"), LyricLine(12, "下一句"))), 2.)
        with patch.object(view, "update") as repaint:
            for position in (2.1, 2.2, 3.):
                view.set_lyric_time(position)
            repaint.assert_not_called()
        view.set_lyrics(Lyrics((LyricLine(1, "月光" * 60), LyricLine(12, "下一句"))), 2.)
        with patch.object(view, "update") as repaint:
            view.set_lyric_time(5.)
            repaint.assert_called_once()
            pan = view.pan_position
            view.set_lyric_time(5.)
            self.assertEqual(view.pan_position, pan)
            self.assertEqual(repaint.call_count, 1)
            view.set_lyric_time(1.)
            self.assertEqual(view.pan_position, (0, 0.))
        self.control.set_playback(PlaybackSnapshot(status="paused", position=9.5, duration=10))
        self.control.glass.view.set_lyrics(Lyrics((LyricLine(1, "月光" * 80),)), 10.)
        self.control.update_playback_display()
        self.assertEqual(view.lyric_end_time, 11.)
        self.assertEqual(view.pan_position, (0, 1.))

    def test_live_switch_covers_static_dynamic_and_tray_text_without_searching_again(self):
        self.control.set_playback(PlaybackSnapshot(title="等待 Apple Music", artist="歌手 Artist 가수", album="繁體專輯",
                                                  status="paused", position=30, duration=180))
        document = Lyrics((LyricLine(1, "歌詞 그대로"),), "网易云音乐", "等待 Apple Music", "歌手 Artist 가수")
        self.control.accept_lyrics(self.control.lyric_request, document, "已匹配 · 网易云音乐 · 1 行（缓存）")
        self.control.sample()
        order = self.control.settings["lyric_source_order"]
        with patch.object(self.control, "request_lyrics") as search:
            for language in ("en", "ja", "ko", "zh_TW", "zh_CN"):
                self.language(language)
                self.assertEqual(self.control.retry_lyrics.text(), translate(language, "重新匹配"))
                self.assertEqual(self.control.clear_lyrics_cache.text(), translate(language, "清空歌词缓存"))
                self.assertEqual(self.control.position_buttons[1].text(), translate(language, "顶部居中"))
                self.assertEqual(self.control.tray_menu.actions()[0].text(), translate(language, "打开设置"))
                self.assertEqual(self.control.storefront.currentText(), translate(language, "土耳其"))
                self.assertEqual(self.control.media_status.text(), translate(language, "已暂停"))
                self.assertEqual(self.control.lyric_timing.text(), translate(language, "标准"))
                self.assertEqual(self.control.track_title.text(), "等待 Apple Music", "A song title was translated as UI text")
                self.assertEqual(self.control.track_details.text(), "歌手 Artist 가수 · 繁體專輯")
                self.assertIs(self.control.glass.view.lyrics, document)
                self.assertEqual(self.control.settings["lyric_source_order"], order)
                self.assertEqual(self.control.source_choices[0].accessibleName(),
                                 translate(language, "第 {position} 优先歌词源", position=1))
                self.assertIn(__version__, self.control.version_label.text())
                self.assertEqual(self.control.lyrics_status.text(), translate_message(language, self.control.lyrics_message))
            search.assert_not_called()
        self.language("en")
        self.assertIn("Memory", self.control.stats.text())
        self.assertIn("Desktop capture", self.control.timing.text())

    def test_status_error_and_pause_states_survive_language_switch(self):
        self.control.failed("无效的桌面取样尺寸")
        self.language("en")
        self.assertEqual(self.control.status.text(), "Refresh stopped: Invalid desktop capture size")
        self.assertEqual(self.control.pause.text(), "Retry refresh")
        self.assertEqual(self.control.status.objectName(), "errorStatus")
        self.control.capture_status_changed(True)
        self.language("ko")
        self.assertEqual(self.control.status.text(), translate("ko", "桌面取样暂时不可用，正在自动恢复…"))
        self.assertEqual(self.control.pause.text(), translate("ko", "继续刷新"))

    def test_source_names_fit_after_switching_languages_at_minimum_window_width(self):
        self.control.move(-30000, -30000)
        self.control.resize(self.control.minimumWidth(), self.control.minimumHeight())
        self.control.show()
        for language in LANGUAGES:
            self.language(language)
            self.app.processEvents()
            self.assertLessEqual(self.control.scroll.widget().width(), self.control.scroll.viewport().width())
            for choice in self.control.source_choices:
                option = QStyleOptionComboBox(); choice.initStyleOption(option)
                rect = choice.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                                     QStyle.SubControl.SC_ComboBoxEditField, choice)
                self.assertGreaterEqual(rect.width(), choice.fontMetrics().horizontalAdvance(choice.currentText()),
                                        f"{language}: {choice.currentText()} is clipped")

    def test_width_keyboard_drag_units_and_boundaries_are_ten_pixels(self):
        slider = self.control.sliders["width"]
        self.assertEqual(self.control.settings["width"], 580)
        QTest.keyClick(slider, Qt.Key.Key_Right)
        self.assertEqual(self.control.settings["width"], 590)
        QTest.keyClick(slider, Qt.Key.Key_Left)
        self.assertEqual(self.control.settings["width"], 580)
        for value in range(slider.minimum(), slider.maximum() + 1):
            slider.setValue(value)
            self.assertEqual(self.control.settings["width"], value * 10)
            self.assertEqual(self.control.glass.width(), value * 10)
        self.assertEqual(self.control.settings["width"], 960)

    def test_120_fps_is_selectable_and_preserves_capture_recovery_rate(self):
        self.control.glass.paused = False
        self.control.fps.setCurrentIndex(self.control.fps.findData(120))
        self.assertEqual(self.control.settings["fps"], 120)
        self.assertTrue(self.control.glass.timer.isActive())
        self.assertEqual(self.control.glass.timer.interval(), round(1000 / 120))
        self.control.glass.retry_delay = 5000
        self.control.glass.timer.start(5000)
        self.control.fps.setCurrentIndex(self.control.fps.findData(60))
        self.assertEqual(self.control.glass.timer.interval(), 5000)

    def test_calibration_labels_use_the_existing_baseline_as_standard(self):
        self.assertEqual(self.control.lyric_timing.text(), "标准")
        self.assertEqual(self.control.settings["lyric_offset_ms"], 1000)
        self.control.change_lyric_offset(100)
        self.assertEqual(self.control.lyric_timing.text(), "提前 0.1 秒")
        self.control.change_lyric_offset(-200)
        self.assertEqual(self.control.lyric_timing.text(), "延后 0.1 秒")
        self.assertNotIn("1 秒", self.control.lyric_timing.toolTip())
        self.control.change_lyric_offset(100)
        self.language("en")
        self.assertEqual(self.control.lyric_timing.text(), "Standard")

    def test_all_six_positions_use_the_current_monitor_available_area(self):
        bounds = QRect(-1920, -180, 1920, 1040)
        screen = SimpleNamespace(availableGeometry=lambda: bounds)
        self.control.glass.resize(580, 80)
        expected = [(-1896, -156), (-1250, -156), (-604, -156),
                    (-1896, 756), (-1250, 756), (-604, 756)]
        with patch.object(native.QApplication, "screenAt", return_value=screen):
            for button, point in zip(self.control.position_buttons, expected):
                button.click()
                self.assertEqual((self.control.glass.x(), self.control.glass.y()), point)
        self.assertTrue(self.control.glass.paused, "Quick positioning resumed a paused capture")

    def test_cache_clear_preserves_display_and_translates_async_feedback(self):
        document = Lyrics((LyricLine(1, "月光"), LyricLine(2, "별")), "酷狗", "Moon", "Luna")
        self.control.glass.view.set_lyrics(document)
        self.control.lyrics_worker = Mock()
        self.control.clear_lyrics_cache.setEnabled(True)
        previous = self.control.lyric_request
        self.control.clear_lyrics_cache.click()
        self.control.lyrics_worker.clear_cache.assert_called_once()
        self.assertFalse(self.control.clear_lyrics_cache.isEnabled())
        self.assertGreater(self.control.lyric_request, previous)
        self.assertIs(self.control.glass.view.lyrics, document)
        self.language("en")
        self.assertEqual(self.control.cache_status.text(), "Clearing lyric cache…")
        self.control.cache_cleared(True)
        self.assertTrue(self.control.clear_lyrics_cache.isEnabled())
        self.assertEqual(self.control.cache_status.text(), "Lyric cache cleared")
        self.control.cache_cleared(False)
        self.assertEqual(self.control.cache_status.text(), "Could not clear the cache; try again later")
        self.assertIs(self.control.glass.view.lyrics, document)
        self.control.lyrics_worker = None


if __name__ == "__main__":
    unittest.main()
