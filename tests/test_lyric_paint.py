"""Check lyric readability, viewport bounds and playback-driven long-line movement."""
import sys
import unittest

if sys.platform == "win32":
    from PIL import Image, ImageStat
    from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QImage
    from PyQt6.QtWidgets import QApplication
    from moonlyrics.lyric_paint import load_bundled_font, lyric_font, lyric_layer, lyric_pan
    from moonlyrics.lyrics import Lyrics, LyricLine
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == "win32", "Qt lyric rendering")
class LyricPaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.family = DEFAULTS["lyric_font"]

    def rendered(self, document, position, width=580, height=80, dpr=1., family=None):
        family = family or self.family
        layer = lyric_layer(document, document.scroll_at(position), width, height, dpr, False,
                            family, lyric_pan(document, position, width, family))
        rgba = layer.convertToFormat(QImage.Format.Format_RGBA8888)
        return Image.frombytes("RGBA", (rgba.width(), rgba.height()), rgba.bits().asstring(rgba.sizeInBytes()))

    def test_bundled_default_loads_the_black_face_without_changing_other_fonts(self):
        font_id = load_bundled_font()
        self.assertGreaterEqual(font_id, 0, "The bundled font is missing or invalid")
        self.addCleanup(QFontDatabase.removeApplicationFont, font_id)
        self.assertIn(DEFAULTS["lyric_font"], QFontDatabase.applicationFontFamilies(font_id))
        self.assertEqual(DEFAULTS["lyric_font"], "Noto Sans SC Black")
        info = QFontInfo(lyric_font(None))
        self.assertEqual(info.family(), "Noto Sans SC Black")
        self.assertEqual(info.weight(), QFont.Weight.Black)
        self.assertEqual(lyric_font("Segoe UI").weight(), QFont.Weight.Medium)

    def test_fonts_and_display_scaling_keep_text_inside_a_faded_viewport(self):
        document = Lyrics((LyricLine(1, "上句淡去"), LyricLine(8, "月光 Moonlight 달빛 月明かり"),
                           LyricLine(16, "下句浮现")))
        for width, height, dpr, family in ((320, 80, 1., self.family), (580, 80, 1.25, "Segoe UI"),
                                          (580, 80, 2., "Consolas"), (960, 240, 1., "Missing Moon Font")):
            with self.subTest(width=width, height=height, dpr=dpr, family=family):
                alpha = self.rendered(document, 9, width, height, dpr, family).getchannel("A")
                self.assertGreater(alpha.getextrema()[1], 200)
                bounds = alpha.getbbox()
                self.assertGreaterEqual(bounds[0], round(24 * dpr) - 1)
                self.assertLessEqual(bounds[2], round((width - 24) * dpr) + 1)
                self.assertEqual(alpha.crop((0, 0, alpha.width, round(5 * dpr))).getextrema()[1], 0)
                self.assertEqual(alpha.crop((0, alpha.height - round(5 * dpr), alpha.width, alpha.height)).getextrema()[1], 0)
                areas = [ImageStat.Stat(alpha.crop((0, round(alpha.height * a), alpha.width,
                                                  round(alpha.height * b)))).mean[0]
                         for a, b in ((0., .28), (.34, .66), (.72, 1.))]
                self.assertGreater(areas[1], max(areas[0], areas[2]) * 1.5)

    def test_long_lines_keep_the_same_glyph_height_and_show_different_sections(self):
        short = Lyrics((LyricLine(0, "MMMM"),))
        long = Lyrics((LyricLine(0, "M" * 70 + "nnnnnn"), LyricLine(12, "Next")))
        first, last = self.rendered(long, .1, 320), self.rendered(long, 11, 320)
        short_bounds = self.rendered(short, .1, 320).getchannel("A").crop((0, 24, 320, 56)).getbbox()
        long_bounds = first.getchannel("A").crop((0, 24, 320, 56)).getbbox()
        self.assertEqual(short_bounds[3] - short_bounds[1], long_bounds[3] - long_bounds[1])
        self.assertNotEqual(first.tobytes(), last.tobytes())
        self.assertEqual(lyric_font(self.family).pixelSize(), 26)

    def test_horizontal_motion_holds_both_ends_and_follows_pause_seek_and_cues(self):
        document = Lyrics((LyricLine(1, "月光" * 40), LyricLine(11, "下一句")))
        samples = [lyric_pan(document, 1 + n / 10, 320, self.family)[1] for n in range(100)]
        self.assertEqual(samples[0], 0.)
        self.assertEqual(samples[-1], 1.)
        self.assertTrue(all(a <= b for a, b in zip(samples, samples[1:])))
        midpoint = lyric_pan(document, 5.9, 320, self.family)
        self.assertAlmostEqual(midpoint[1], .5)
        self.assertEqual(lyric_pan(document, 5.9, 320, self.family), midpoint)
        self.assertEqual(lyric_pan(document, 1, 320, self.family), (0, 0.))
        self.assertEqual(lyric_pan(document, 11, 320, self.family), (1, 0.))
        self.assertEqual(lyric_pan(None, 0, 320, self.family), (-1, 0.))
        self.assertEqual(lyric_pan(Lyrics(()), 0, 320, self.family), (-1, 0.))
        self.assertEqual(lyric_pan(Lyrics((document.lines[0],)), 1000, 320, self.family), (0, 1.))

    def test_final_long_line_finishes_before_the_known_song_end(self):
        document = Lyrics((LyricLine(100, "月光" * 80),))
        self.assertLess(lyric_pan(document, 102.5, 320, self.family)[1], 1.)
        self.assertEqual(lyric_pan(document, 102.5, 320, self.family, end_time=103), (0, 1.))
        self.assertEqual(lyric_pan(document, 100, 320, self.family, end_time=103), (0, 0.))

    def test_short_lines_remain_stationary_and_font_choice_changes_the_rendering(self):
        document = Lyrics((LyricLine(1, "Moonlight WWWiii"), LyricLine(10, "Next")))
        for position in (1, 3, 8):
            self.assertEqual(lyric_pan(document, position, 580, self.family), (0, 0.))
        self.assertEqual(self.rendered(document, 2).tobytes(), self.rendered(document, 7).tobytes())
        self.assertNotEqual(self.rendered(document, 2).tobytes(),
                            self.rendered(document, 2, family="Consolas").tobytes())


if __name__ == "__main__":
    unittest.main()
