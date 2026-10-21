import json
import unittest
import moonlyrics.styles as style_settings

from moonlyrics.styles import DEFAULTS, PRESETS, normalized, transparency_amount


class StyleTests(unittest.TestCase):
    def test_lyric_font_round_trips_and_invalid_preferences_use_default(self):
        for family in ("Microsoft YaHei UI", "Yu Gothic UI", "맑은 고딕", "A missing font"):
            settings = normalized(dict(lyric_font=family))
            self.assertEqual(normalized(json.loads(json.dumps(settings)))["lyric_font"], family)
        self.assertEqual(normalized(dict(lyric_font="  Segoe UI  "))["lyric_font"], "Segoe UI")
        for value in (None, [], 42, "", " ", "bad\x00font", "bad\nfont", "x" * 129):
            self.assertEqual(normalized(dict(lyric_font=value))["lyric_font"], DEFAULTS["lyric_font"])

    def test_all_sources_default_to_one_second_and_allow_adjustment_and_reset(self):
        from moonlyrics.lyrics import Lyrics, LyricLine
        document = Lyrics((LyricLine(1, "月光"), LyricLine(2, "落下")))
        for source in style_settings.LYRIC_SOURCES:
            settings = normalized(dict(lyric_source_order=[source] + [s for s in style_settings.LYRIC_SOURCES if s != source]))
            self.assertEqual(document.index_at(.1 + style_settings.lyric_offset_seconds(settings)), 0)
            settings["lyric_offset_ms"] = 1300
            self.assertAlmostEqual(style_settings.lyric_offset_seconds(settings), 1.3)
            settings["lyric_offset_ms"] = 0
            self.assertEqual(style_settings.lyric_offset_seconds(settings), 0)
            settings["lyric_offset_ms"] = DEFAULTS["lyric_offset_ms"]
            self.assertEqual(style_settings.lyric_offset_seconds(settings), 1)

    def test_fixed_rich_material_migrates_legacy_styles_and_preserves_controls(self):
        self.assertEqual(set(PRESETS), {"rich"})
        for previous in ("clear", "soft", "rich", "unknown", [], None):
            result = normalized(dict(preset=previous, blur=100, refraction=100, width=640, transparency_level=7))
            self.assertEqual(result["preset"], "rich")
            self.assertEqual(result["blur"], PRESETS["rich"]["blur"])
            self.assertEqual(result["refraction"], PRESETS["rich"]["refraction"])
            self.assertEqual(result["width"], 640)
            self.assertEqual(result["transparency_level"], 7)
            self.assertEqual(normalized(json.loads(json.dumps(result))), result)

    def test_transparency_mapping_and_legacy_settings(self):
        self.assertEqual(transparency_amount(1), .9)
        self.assertEqual(transparency_amount(10), 1.)
        self.assertEqual(normalized(dict(transparency=90))["transparency_level"], 1)
        self.assertEqual(normalized(dict(transparency=100))["transparency_level"], 10)
        self.assertEqual(normalized(dict(transparency_level=4, transparency=100))["transparency_level"], 4)
        amounts = [transparency_amount(i) for i in range(1, 11)]
        self.assertTrue(all(a < b for a, b in zip(amounts, amounts[1:])))

    def test_invalid_settings_and_bounds(self):
        self.assertEqual(normalized(None), DEFAULTS)
        self.assertEqual(normalized(dict(preset=[], fps=1000, width=float("nan"))), DEFAULTS)
        result = normalized(dict(transparency_level=100, width=-1, height=1000))
        self.assertEqual((result["transparency_level"], result["width"], result["height"]), (10, 320, 240))

    def test_lyric_calibration_is_preserved_and_bounded(self):
        self.assertEqual(normalized({})["lyric_offset_ms"], 1000)
        self.assertEqual(normalized(dict(lyric_offset_ms=300))["lyric_offset_ms"], 300)
        self.assertEqual(normalized(dict(lyric_offset_ms=-9000))["lyric_offset_ms"], -3000)
        self.assertEqual(normalized(dict(lyric_offset_ms=9000))["lyric_offset_ms"], 3000)

    def test_timing_upgrade_is_once_only_and_does_not_double_a_manual_one_second(self):
        for previous, expected in ((0, 1000), (1000, 1000), (300, 300), (-400, -400)):
            settings = normalized(dict(lyric_offset_ms=previous))
            self.assertEqual(settings["lyric_offset_ms"], expected)
            self.assertEqual(normalized(json.loads(json.dumps(settings))), settings)
        explicit_zero = normalized(dict(lyric_offset_ms=0, lyric_timing_version=1))
        self.assertEqual(explicit_zero["lyric_offset_ms"], 0)
        self.assertEqual(normalized(json.loads(json.dumps(explicit_zero))), explicit_zero)

    def test_lyric_source_order_is_saved_and_invalid_orders_use_defaults(self):
        order = ("qq", "netease", "lrclib", "kugou")
        settings = normalized(dict(lyric_source_order=order))
        self.assertEqual(normalized(json.loads(json.dumps(settings)))["lyric_source_order"], order)
        for invalid in (None, [], "qq", ["qq"] * 4, ["qq", "netease", "lrclib", "unknown"],
                        ["qq", "netease", "lrclib", []]):
            self.assertEqual(normalized(dict(lyric_source_order=invalid))["lyric_source_order"], DEFAULTS["lyric_source_order"])


if __name__ == "__main__":
    unittest.main()
