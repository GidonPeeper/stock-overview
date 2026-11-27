"""The base and secondary colour chosen in Settings (frontend/colors.js), run
in node: whatever colour is picked, text and buttons stay readable in both
themes.

The contrast is checked here with its own WCAG formula, not the one in
colors.js: AA asks 4.5:1 for text and 3:1 for the edge of a control.
"""

import json
import random
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

# The surfaces of the pages (the CSS tokens --bg, --panel, --panel2).
SURFACES = {"light": ["#f4f6f9", "#ffffff", "#eff2f6"], "dark": ["#0a0d10", "#12171e", "#1a212b"]}


def lum(h: str) -> float:
    def ch(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    x, y = sorted((lum(a), lum(b)))
    return (y + 0.05) / (x + 0.05)


def over(rgba: str, under: str) -> str:
    """An rgba(...) tint seen over a solid colour, as the browser blends it."""
    r, g, b, a = (float(v) for v in re.findall(r"[\d.]+", rgba))
    u = [int(under[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(c + (t - c) * a):02x}" for c, t in zip(u, (r, g, b)))


@unittest.skipIf(NODE is None, "node is not installed")
class ColorsTest(unittest.TestCase):
    def js(self, code: str):
        src = (ROOT / "frontend" / "colors.js").as_posix()
        out = subprocess.run([NODE, "-e", f"require({json.dumps(src)}); const C=globalThis.SOColors; {code}"],
                             capture_output=True, text=True, timeout=30, env={"PATH": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def derive_all(self, colors: list[str]) -> dict:
        return self.js(f"console.log(JSON.stringify(Object.fromEntries({json.dumps(colors)}.map(c => [c, C.derive(c)]))))")

    def check(self, color: str, d: dict):
        for theme, grounds in SURFACES.items():
            v = d[theme]
            tinted = [over(v["tint"], g) for g in grounds]
            for g in grounds + tinted:
                self.assertGreaterEqual(contrast(v["text"], g), 4.5, f"{color} {theme}: text {v['text']} on {g}")
            for g in grounds:
                self.assertGreaterEqual(contrast(v["fill"], g), 3.0, f"{color} {theme}: button {v['fill']} on {g}")
            self.assertGreaterEqual(contrast(v["on"], v["fill"]), 4.5, f"{color} {theme}: {v['on']} on {v['fill']}")

    def test_hard_colours_stay_readable(self):
        # yellow is too light for the light theme, near-black too dark for the
        # dark one, near-white too light for the light one; mid grey and the
        # pure primaries sit where neither black nor white text is easy.
        hard = ["#ffff00", "#facc15", "#f5c518", "#0a0a0a", "#000000", "#111827", "#f5f5f5", "#ffffff",
                "#808080", "#777777", "#ff0000", "#00ff00", "#0000ff", "#00ffff", "#ff00ff", "#2563eb"]
        for color, d in self.derive_all(hard).items():
            self.check(color, d)

    def test_any_colour_stays_readable(self):
        rnd = random.Random(7)
        colors = ["#%06x" % rnd.randrange(1 << 24) for _ in range(300)]
        for color, d in self.derive_all(colors).items():
            self.check(color, d)

    def test_presets_are_readable_and_named(self):
        presets = self.js("console.log(JSON.stringify(C.PRESETS))")
        self.assertGreaterEqual(len(presets), 4)
        self.assertEqual(presets[0]["name"], "Standard")
        for p in presets:
            self.assertTrue(p["name"])
            for color, d in self.derive_all([p["base"], p["secondary"]]).items():
                self.check(color, d)

    def test_a_colour_that_already_reads_well_is_kept(self):
        # #2563eb on white is 5.2:1, so the light theme's button keeps it exactly;
        # yellow on the dark page is far above 4.5:1, so it stays yellow there.
        d = self.derive_all(["#2563eb", "#facc15"])
        self.assertEqual(d["#2563eb"]["light"]["fill"], "#2563eb")
        self.assertEqual(d["#2563eb"]["light"]["on"], "#ffffff")
        self.assertEqual(d["#facc15"]["dark"]["text"], "#facc15")
        self.assertNotEqual(d["#facc15"]["dark"]["on"], "#ffffff")       # dark text on a yellow button

    def test_css_only_overrides_what_was_chosen(self):
        out = self.js('console.log(JSON.stringify([C.css("", ""), C.css("#2563eb", ""), C.css("", "#7c3aed"), C.css("#2563eb", "#7c3aed")]))')
        self.assertEqual(out[0], "")
        self.assertIn("--accent:", out[1])
        self.assertNotIn("--accent2", out[1])
        self.assertNotIn("--accent:", out[2])
        self.assertIn("--accent2-fill:", out[2])
        self.assertTrue(out[3].startswith(":root{") and ':root[data-theme="light"]{' in out[3])

    def test_bad_colours_are_refused(self):
        out = self.js('let e=[]; for (const c of ["red", "#12345", "#gggggg", ""]) { try { C.derive(c); e.push(false); } catch (x) { e.push(true); } } console.log(JSON.stringify(e))')
        self.assertEqual(out, [True, True, True, True])

    def test_pages_ship_the_standard_colours(self):
        # The CSS defaults of the three pages are what derive() makes of the
        # standard pair, so "Back to the standard colours" looks the same as
        # never having chosen.
        css = self.js("console.log(JSON.stringify(C.css(C.DEFAULTS.base, C.DEFAULTS.secondary)))")
        want = dict(re.findall(r"--([\w-]+):([^;]+);", css.split("}:root")[0]))
        want_light = dict(re.findall(r"--([\w-]+):([^;]+);", css.split('[data-theme="light"]{')[1]))
        for page in ("index.html", "settings.html", "login.html"):
            html = (ROOT / "frontend" / page).read_text()
            dark = re.search(r":root\{([^}]*)\}", html).group(1)
            light = re.search(r':root\[data-theme="light"\]\{([^}]*)\}', html).group(1)
            for block, expect in ((dark, want), (light, want_light)):
                got = dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))
                for k, v in expect.items():
                    if k in got:   # login.html carries only the tokens it uses
                        self.assertEqual(got[k].replace(" ", ""), v.replace(" ", ""), f"{page} --{k}")
                self.assertIn("accent", got, page)


if __name__ == "__main__":
    unittest.main()
