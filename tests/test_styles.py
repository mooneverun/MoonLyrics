import json
import unittest
import moonlyrics.styles as style_settings

from moonlyrics.styles import DEFAULTS, PRESETS, normalized


class StyleTests(unittest.TestCase):
    def test_appearance_modes_round_trip_and_migrate_old_transparency(self):
        self.assertEqual(DEFAULTS["appearance_mode"], "system")
        for mode in ("day", "night", "system"):
            settings = normalized(dict(appearance_mode=mode, transparency_level=47, transparency_scale=100))
            self.assertEqual(settings["appearance_mode"], mode)
            self.assertNotIn("transparency_level", settings)
            self.assertEqual(normalized(json.loads(json.dumps(settings))), settings)
        for invalid in (None, [], "unknown"):
            self.assertEqual(normalized(dict(appearance_mode=invalid))["appearance_mode"], "system")
        previous = normalized(dict(transparency_level=47, transparency_scale=100, lyric_font="Segoe UI", width=640))
        self.assertEqual((previous["appearance_mode"], previous["lyric_font"], previous["width"]),
                         ("system", "Segoe UI", 640))

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

    def test_invalid_settings_and_bounds(self):
        self.assertEqual(normalized(None), DEFAULTS)
        self.assertEqual(normalized(dict(preset=[], fps=1000, width=float("nan"))), DEFAULTS)
        result = normalized(dict(transparency_level=100, width=-1, height=1000))
        self.assertEqual((result["width"], result["height"]), (320, 240))

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
        order = ("qq", "netease", "lrclib")
        settings = normalized(dict(lyric_source_order=order))
        self.assertEqual(normalized(json.loads(json.dumps(settings)))["lyric_source_order"], order)
        self.assertEqual(normalized(dict(lyric_source_order=order+("kugou",)))["lyric_source_order"], order)
        for reserve in style_settings.LYRIC_SOURCES:
            active = tuple(source for source in style_settings.LYRIC_SOURCES if source != reserve)
            self.assertEqual(normalized(dict(lyric_source_order=active))["lyric_source_order"], active)
        for invalid in (None, [], "qq", ["qq"] * 4, ["qq", "netease", "lrclib", "unknown"],
                        ["qq", "netease", "lrclib", []]):
            self.assertEqual(normalized(dict(lyric_source_order=invalid))["lyric_source_order"], DEFAULTS["lyric_source_order"])


if __name__ == "__main__":
    unittest.main()
