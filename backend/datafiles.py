"""Locate the user's private data files across deployment styles.

Locally the real files live in `data/`. On Render, Secret Files are mounted at
the service root and `/etc/secrets/` — NOT inside `data/` — which silently sent
deployments into demo mode. This resolver checks every sensible location and
reports what it found, so the UI can show which sources are live vs sample.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

_SEARCH_DIRS = [
    Path(os.environ["STOCK_DATA_DIR"]) if os.environ.get("STOCK_DATA_DIR") else None,
    DATA_DIR,                # local development
    ROOT,                    # Render secret files (repo root)
    Path("/etc/secrets"),    # Render secret files (canonical mount)
]


def resolve(name: str) -> tuple[Path, bool]:
    """Return (path, is_real). Falls back to data/<stem>.sample<suffix>.

    Matching is forgiving: exact name first, then any file in the search dirs
    whose (lowercased) name contains the expected stem — so `Degiro_Account (1).csv`
    or an oddly-renamed secret file still gets picked up.
    """
    stem, suffix = name.rsplit(".", 1)
    for d in _SEARCH_DIRS:
        if d is not None and (d / name).exists():
            return d / name, True
    for d in _SEARCH_DIRS:
        if d is None or not d.is_dir():
            continue
        try:
            for p in d.iterdir():
                n = p.name.lower()
                if (p.is_file() and stem.lower() in n and n.endswith("." + suffix)
                        and ".sample." not in n):
                    return p, True
        except OSError:
            continue
    return DATA_DIR / f"{stem}.sample.{suffix}", False


EXPECTED = [("degiro", "degiro_account.csv"),
            ("trade_republic", "trades_trade_republic.json"),
            ("tr_income", "income_trade_republic.json"),
            ("t212_statement", "trading212_statement.json")]


def status() -> dict:
    """Which data sources are real vs sample — surfaced at /api/datastatus.
    Includes a directory listing of the search locations (names only) so a
    misnamed upload/secret file is diagnosable from the UI."""
    out: dict = {}
    for key, fname in EXPECTED:
        path, real = resolve(fname)
        out[key] = {"real": real, "path": str(path), "expected": fname}
    from .connectors.trading212 import accounts  # lazy: avoids an import cycle

    labels = [a.label for a in accounts()]
    live = bool(labels)
    out["trading212"] = {
        # live API (one or more accounts), or the imported activity statement
        "real": live or out["t212_statement"]["real"],
        "live": live,
        "accounts": labels,
        "path": "API: " + ", ".join(labels) if live else out["t212_statement"]["path"],
        "expected": "T212_API_KEY + T212_API_SECRET (or T212_<NAME>_API_KEY/SECRET per account), "
                    "or trading212_statement.json",
    }
    from .connections import load_all  # accounts added in Settings ("Add an account")

    conns = [c["label"] for c in load_all()]
    out["connections"] = {"real": bool(conns), "accounts": conns, "path": "connections.json",
                          "expected": "accounts added in Settings → Add an account"}
    seen: dict[str, list[str]] = {}
    for d in _SEARCH_DIRS:
        if d is None or not d.is_dir():
            continue
        try:
            names = sorted(p.name for p in d.iterdir()
                           if p.is_file() and p.suffix in (".csv", ".json")
                           and ".sample." not in p.name)[:20]
            if names:
                seen[str(d)] = names
        except OSError:
            continue
    out["_files_seen"] = seen
    return out


def save_upload(kind: str, content: bytes) -> Path:
    """Persist an uploaded statement to data/ (kind = key from EXPECTED).
    On free hosting the disk is ephemeral (re-upload after a redeploy), but it
    takes effect instantly — no dashboard fiddling."""
    fname = dict(EXPECTED).get(kind)
    if not fname:
        raise ValueError(f"unknown upload kind: {kind}")
    return save_file(fname, content)


def save_file(fname: str, content: bytes) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / fname
    dest.write_bytes(content)
    return dest
