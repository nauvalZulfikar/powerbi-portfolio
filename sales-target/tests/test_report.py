"""Report palette tests: status colours must stay readable (WCAG 2.x contrast)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import build_report as r  # noqa: E402


def lum(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class PaletteTests(unittest.TestCase):
    def test_text_colours_clear_aa(self):
        for name in ("ON_C", "RISK_C", "BEHIND_C", "NO_STATUS", "MUTED", "INK2"):
            self.assertGreaterEqual(ratio(getattr(r, name), r.SURFACE), 4.5, name)

    def test_target_line_clears_graphics_contrast(self):
        self.assertGreaterEqual(ratio(r.TARGET_C, r.SURFACE), 3.0)

    def test_theme_series_colours_clear_graphics_contrast(self):
        for c in r.THEME["dataColors"]:
            self.assertGreaterEqual(ratio(c, r.SURFACE), 3.0, c)

    def test_theme_sentiment_colours_match_status(self):
        self.assertEqual((r.THEME["good"], r.THEME["neutral"], r.THEME["bad"]), (r.ON_C, r.RISK_C, r.BEHIND_C))


if __name__ == "__main__":
    unittest.main()
