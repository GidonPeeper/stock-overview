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
    """Return (path, is_real). Falls back to data/<stem>.sample<suffix>."""
    for d in _SEARCH_DIRS:
        if d is not None:
            p = d / name
            if p.exists():
                return p, True
    stem, suffix = name.rsplit(".", 1)
    return DATA_DIR / f"{stem}.sample.{suffix}", False


def status() -> dict:
    """Which data sources are real vs sample — surfaced at /api/datastatus."""
    out = {}
    for key, fname in [("degiro", "degiro_account.csv"),
                       ("trade_republic", "trades_trade_republic.json"),
                       ("tr_income", "income_trade_republic.json")]:
        path, real = resolve(fname)
        out[key] = {"real": real, "path": str(path)}
    out["trading212"] = {
        "real": bool(os.getenv("T212_API_KEY") and os.getenv("T212_API_SECRET")),
        "path": "env:T212_API_KEY/SECRET",
    }
    return out
