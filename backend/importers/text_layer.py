"""Recover the text of Trading 212 statement PDFs whose fonts hide their digits.

Trading 212 statements embed Type3 fonts whose ToUnicode table simply omits the
digits (and a few letters), so every PDF library reads "€?,???.??". The glyph
*drawings* are still there, one PostScript-like program per glyph, and the same
character is always drawn by the same program. So we identify each unmapped
glyph by the SHA-1 of its program and look the character up in a small table
learned once (from an OCR pass over a known statement, cross-checked against
the statement's own totals). Deterministic, offline, runs anywhere pdfplumber
does — no OCR at import time.

Unseen glyph programs (a new font weight, say) fall back to the glyph *name*
(Trading 212's fonts keep stable glyph indices across weights); if that fails
too the character is left as "?" and reported, so nothing is silently guessed.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

_TABLE_PATH = Path(__file__).with_name("t212_glyphs.json")
_PUA_BASE = 0xF0000  # supplementary private-use plane: room for 256 fonts x 256 codes


def _load_table() -> tuple[dict[str, str], dict[str, str]]:
    if not _TABLE_PATH.exists():
        return {}, {}
    t = json.loads(_TABLE_PATH.read_text())
    return t.get("by_hash", {}), t.get("by_name", {})


class _Patch:
    """Context manager: makes pdfminer emit a private-use code point for every
    glyph it cannot map, remembering which font/glyph it came from."""

    def __enter__(self):
        from pdfminer.pdffont import PDFType3Font

        self.fonts: list = []
        self._orig_init = PDFType3Font.__init__
        self._orig_to_unichr = PDFType3Font.to_unichr
        fonts = self.fonts

        def init(font, rsrcmgr, spec):
            self._orig_init(font, rsrcmgr, spec)
            font._t212_spec = spec
            font._t212_sid = len(fonts)
            fonts.append(font)

        orig = self._orig_to_unichr

        def to_unichr(font, cid):
            try:
                u = orig(font, cid)
            except Exception:
                u = "\x00"
            if u == "\x00" and hasattr(font, "_t212_sid"):
                return chr(_PUA_BASE + font._t212_sid * 256 + cid)
            return u

        PDFType3Font.__init__ = init
        PDFType3Font.to_unichr = to_unichr
        self._cls = PDFType3Font
        return self

    def __exit__(self, *exc):
        self._cls.__init__ = self._orig_init
        self._cls.to_unichr = self._orig_to_unichr

    def glyph(self, codepoint: int) -> tuple[str, str]:
        """(sha1-16 of the glyph program, glyph name) for a private-use code."""
        from pdfminer.pdftypes import resolve1
        from pdfminer.psparser import PSLiteral

        sid, cid = divmod(codepoint - _PUA_BASE, 256)
        spec = self.fonts[sid]._t212_spec
        enc = resolve1(spec.get("Encoding"))
        diffs = resolve1(enc.get("Differences")) if isinstance(enc, dict) else None
        name, code = None, None
        for item in diffs or []:
            if isinstance(item, int):
                code = item
            else:
                if code == cid:
                    name = item.name if isinstance(item, PSLiteral) else str(item)
                    break
                code += 1
        data = b""
        if name:
            procs = resolve1(spec.get("CharProcs")) or {}
            if name in procs:
                data = resolve1(procs[name]).get_data()
        return hashlib.sha1(data).hexdigest()[:16], (name or "")


def extract_pages(source, table: tuple[dict, dict] | None = None,
                  learn: dict | None = None) -> tuple[list[dict], dict]:
    """PDF (path or bytes) -> (pages, report).

    pages: [{page, width, height, lines:[{top, words:[{text, skel, x0, x1, top,
    bottom}]}]}] where `text` is the decoded word and `skel` keeps '\\x00' for
    each slot that was unmapped in the PDF (useful for auditing).
    `learn`: optional dict; when given, every unmapped glyph occurrence is
    appended as (hash, name, page, line_index, word_index, char_index) so a
    caller can build the lookup table from an independent decoding.
    """
    import pdfplumber

    by_hash, by_name = table if table else _load_table()
    report = {"unmapped_chars": 0, "decoded_by_hash": 0, "decoded_by_name": 0,
              "unknown": 0, "unknown_glyphs": {}}
    pages: list[dict] = []
    with _Patch() as patch:
        opened = pdfplumber.open(io.BytesIO(source) if isinstance(source, bytes) else source)
        with opened as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(x_tolerance=1.5, y_tolerance=2)
                rows: dict[int, list] = {}
                for w in words:
                    key = round(w["top"])
                    k = next((r for r in rows if abs(r - key) <= 2), key)
                    rows.setdefault(k, []).append(w)
                lines = []
                for li, top in enumerate(sorted(rows)):
                    ws = sorted(rows[top], key=lambda w: w["x0"])
                    out_words = []
                    for wi, w in enumerate(ws):
                        skel, text = [], []
                        for ci, ch in enumerate(w["text"]):
                            cp = ord(ch)
                            if _PUA_BASE <= cp < _PUA_BASE + 0x10000:
                                report["unmapped_chars"] += 1
                                h, name = patch.glyph(cp)
                                if learn is not None:
                                    learn.setdefault("occurrences", []).append((h, name, pno, li, wi, ci))
                                if h in by_hash:
                                    dec = by_hash[h]
                                    report["decoded_by_hash"] += 1
                                elif name in by_name:
                                    dec = by_name[name]
                                    report["decoded_by_name"] += 1
                                else:
                                    dec = "?"
                                    report["unknown"] += 1
                                    report["unknown_glyphs"][h] = name
                                skel.append("\x00")
                                text.append(dec)
                            else:
                                skel.append(ch)
                                text.append(ch)
                        out_words.append({"text": "".join(text), "skel": "".join(skel),
                                          "x0": round(w["x0"], 1), "x1": round(w["x1"], 1),
                                          "top": round(w["top"], 1), "bottom": round(w["bottom"], 1)})
                    lines.append({"top": top, "words": out_words})
                pages.append({"page": pno, "width": page.width, "height": page.height, "lines": lines})
    return pages, report


def learn_table(source, decoded_pages: list[dict]) -> dict:
    """Build the glyph table from an independently decoded rendering of the same
    PDF (e.g. OCR). decoded_pages must have the same page/line/word structure
    as extract_pages() output, with each word's `text` fully decoded."""
    from collections import Counter, defaultdict

    learn: dict = {}
    pages, _ = extract_pages(source, table=({}, {}), learn=learn)
    votes: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, Counter] = defaultdict(Counter)
    for h, name, pno, li, wi, ci in learn.get("occurrences", []):
        try:
            dec_word = decoded_pages[pno - 1]["lines"][li]["words"][wi]["text"]
            mine = pages[pno - 1]["lines"][li]["words"][wi]["skel"]
        except (IndexError, KeyError):
            continue
        if len(dec_word) != len(mine):
            continue  # structure drifted: skip this word
        ch = dec_word[ci]
        if ch != "?":
            votes[h][ch] += 1
            names[name][ch] += 1
    by_hash, conflicts = {}, {}
    for h, c in votes.items():
        ch, n = c.most_common(1)[0]
        share = n / sum(c.values())
        if share >= 0.9 and n >= 2:
            by_hash[h] = ch
        else:
            conflicts[h] = dict(c)
    by_name = {}
    for name, c in names.items():
        ch, n = c.most_common(1)[0]
        if n / sum(c.values()) >= 0.9:
            by_name[name] = ch
    return {"by_hash": by_hash, "by_name": by_name, "conflicts": conflicts,
            "votes": {h: dict(c) for h, c in votes.items()}}
