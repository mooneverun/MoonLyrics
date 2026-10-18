import json
import unittest

from moonlyrics.styles import DEFAULTS, PRESETS, normalized, transparency_amount


class StyleTests(unittest.TestCase):
    def test_accepted_presets(self):
        self.assertEqual(set(PRESETS), {"clear", "soft", "rich"})
        for name, preset in PRESETS.items():
            result = normalized(dict(preset=name, blur=100, refraction=100, width=640))
            self.assertEqual(result["blur"], preset["blur"])
            self.assertEqual(result["refraction"], preset["refraction"])
            self.assertEqual(result["width"], 640)
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


if __name__ == "__main__":
    unittest.main()
