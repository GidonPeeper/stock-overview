"""What the user chose in Settings: country, currency, time zone, colours.

Kept in data/settings.json (saved with the rest of the data, see vault.py).
Each choice falls back to the environment variable that set it before, then to
a default, so an existing copy keeps working unchanged:

  country        two-letter country code ("GB", "NL", "US", ...), or "" for
                 none. Drives the tax cards, the tax overview and the news.
                 Fallback: TAX_RESIDENCE (UK/US/NL; anything else = none).
  currency       EUR, GBP or USD: what amounts are shown in.
                 Fallback: DISPLAY_CURRENCY, else EUR.
  timezone       an IANA zone ("Europe/Amsterdam"): when "today" starts.
                 Fallback: DASHBOARD_TZ, else Europe/London.
  color_base, color_secondary
                 "#rrggbb", or "" for the built-in colours.
  news_country   country code for the news, or "" to follow `country`.
  update_check   true to ask GitHub once a day whether a newer version of the
                 app exists (version.py). Off unless switched on in Settings.

Other modules read one value with `prefs.value("country")`. A value that is
not valid is refused on save and ignored on read.
"""

from __future__ import annotations

import json
import os
import re
import threading

from .datafiles import DATA_DIR, write_atomic

FILE = "settings.json"
CURRENCIES = ("EUR", "GBP", "USD")

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_CODE = re.compile(r"^[A-Z]{2}$")
_lock = threading.Lock()
_cache: list = [None, None]     # [file mtime, parsed]


def _country(v) -> str:
    v = str(v or "").strip().upper()
    v = {"UK": "GB", "USA": "US"}.get(v, v)
    if v in ("", "NONE", "OTHER"):
        return ""
    if not _CODE.match(v):
        raise ValueError("The country must be a two-letter code, for example NL")
    return v


def _currency(v) -> str:
    v = str(v or "").strip().upper()
    if v not in CURRENCIES:
        raise ValueError(f"The currency must be one of {', '.join(CURRENCIES)}")
    return v


def _timezone(v) -> str:
    v = str(v or "").strip()
    if not v:
        return ""
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(v)
    except Exception:
        raise ValueError(f"Unknown time zone: {v}") from None
    return v


def _color(v) -> str:
    v = str(v or "").strip()
    if v and not _HEX.match(v):
        raise ValueError("A colour is written as #rrggbb, for example #2563eb")
    return v.lower()


def _switch(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return True
    if s in ("0", "false", "off", "no", ""):
        return False
    raise ValueError("Use true or false")


def _env_country() -> str:
    try:
        return _country(os.getenv("TAX_RESIDENCE") or "UK")
    except ValueError:
        return ""


def _env_currency() -> str:
    c = (os.getenv("DISPLAY_CURRENCY") or "EUR").upper()
    return c if c in CURRENCIES else "EUR"


# key -> (clean a given value or raise ValueError, fallback when not chosen)
_KEYS = {
    "country": (_country, _env_country),
    "currency": (_currency, _env_currency),
    "timezone": (_timezone, lambda: os.getenv("DASHBOARD_TZ") or "Europe/London"),
    "color_base": (_color, lambda: ""),
    "color_secondary": (_color, lambda: ""),
    "news_country": (_country, lambda: ""),
    "update_check": (_switch, lambda: False),
}


def _saved() -> dict:
    """What is in the file ({} when there is none or it cannot be read: the
    choices then fall back, and the next save writes a clean file)."""
    path = DATA_DIR / FILE
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache[0] != mtime:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        _cache[0], _cache[1] = mtime, data if isinstance(data, dict) else {}
    return _cache[1]


def value(key: str) -> str:
    clean, fallback = _KEYS[key]
    saved = _saved()
    if key in saved:
        try:
            return clean(saved[key])
        except ValueError:
            pass
    return fallback()


def get() -> dict:
    """Every choice, with `chosen`: the keys set in Settings (the rest come
    from the environment or the defaults)."""
    saved = _saved()
    return {**{k: value(k) for k in _KEYS}, "chosen": sorted(k for k in _KEYS if k in saved)}


def update(changes: dict) -> dict:
    """Save the given choices (unknown keys are refused). A value of None
    forgets a choice, so it falls back again."""
    unknown = sorted(set(changes) - set(_KEYS))
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(unknown)}")
    cleaned = {k: (None if v is None else _KEYS[k][0](v)) for k, v in changes.items()}
    with _lock:
        data = dict(_saved())
        for k, v in cleaned.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        DATA_DIR.mkdir(exist_ok=True)
        write_atomic(DATA_DIR / FILE, json.dumps(data, indent=2))
        _cache[0] = None
    return get()
