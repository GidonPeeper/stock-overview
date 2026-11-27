"""Locate the user's private data files across deployment styles.

Locally the real files live in `data/`. On Render, Secret Files are mounted at
the service root and `/etc/secrets/` — NOT inside `data/` — which silently sent
deployments into demo mode. This resolver checks every sensible location and
reports what it found, so the UI can show which sources are live vs sample.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

_SEARCH_DIRS = [
    Path(os.environ["STOCK_DATA_DIR"]) if os.environ.get("STOCK_DATA_DIR") else None,
    DATA_DIR,                # local development
    ROOT,                    # Render secret files (repo root)
    Path("/etc/secrets"),    # Render secret files (canonical mount)
]


def _find_real(name: str) -> Path | None:
    """The user's own file for `name` in any search location, or None.
    Matching is forgiving: exact name first, then any file in the search dirs
    whose (lowercased) name contains the expected stem — so `Degiro_Account (1).csv`
    or an oddly-renamed secret file still gets picked up."""
    stem, suffix = name.rsplit(".", 1)
    for d in _SEARCH_DIRS:
        if d is not None and (d / name).exists():
            return d / name
    for d in _SEARCH_DIRS:
        if d is None or not d.is_dir():
            continue
        try:
            for p in d.iterdir():
                n = p.name.lower()
                if (p.is_file() and stem.lower() in n and n.endswith("." + suffix)
                        and ".sample." not in n):
                    return p
        except OSError:
            continue
    return None


# Files that hold the user's own data. Once any of them exists (or a broker key
# is set), the sample files are never used: a friend who only uses Trading 212
# must not see the sample DeGiro account added to their totals.
_OWN_FILES = ("degiro_account.csv", "trades_trade_republic.json", "income_trade_republic.json",
              "trading212_statement.json", "connections.json", "cash_accounts.json",
              "finances.json", "manual_holdings.json")
_T212_ENV = re.compile(r"^T212_(?:[A-Z0-9_]+?_)?API_KEY$")
_own: list = [0.0, None]   # [checked at (monotonic), result]


def has_own_data() -> bool:
    """True once the user has added any data of their own. Checked at most
    every few seconds (it looks at the disk)."""
    now = time.monotonic()
    if _own[1] is None or now - _own[0] > 5:
        own = any(v.strip() for k, v in os.environ.items() if _T212_ENV.match(k)) \
            or any(_find_real(n) for n in _OWN_FILES)
        _own[0], _own[1] = now, own
    return bool(_own[1])


def demo() -> bool:
    """Demo mode: nothing of the user's own yet, so the sample data is shown."""
    return not has_own_data()


def refresh() -> None:
    """Re-check has_own_data() on the next call (after a file was written)."""
    _own[1] = None


def resolve(name: str) -> tuple[Path, bool]:
    """Return (path, is_real). Without a real file: the bundled sample
    (data/<stem>.sample.<suffix>) in demo mode, else a path that does not exist
    (the source then simply has no data)."""
    found = _find_real(name)
    if found is not None:
        return found, True
    if demo():
        stem, suffix = name.rsplit(".", 1)
        return DATA_DIR / f"{stem}.sample.{suffix}", False
    return DATA_DIR / name, False


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
        # removable: the file was uploaded into data/ (not placed elsewhere by hand)
        out[key] = {"real": real, "path": str(path), "expected": fname,
                    "removable": (DATA_DIR / fname).is_file()}
    from .connectors.trading212 import accounts  # lazy: avoids an import cycle

    labels = [a.label for a in accounts()]
    live = bool(labels)
    out["trading212"] = {
        # live API (one or more accounts), or the imported activity statement
        "real": live or out["t212_statement"]["real"],
        "live": live,
        "accounts": labels,
        "path": "API: " + ", ".join(labels) if live else out["t212_statement"]["path"],
        "removable": out["t212_statement"]["removable"],     # the statement, not the keys
        "expected": "T212_API_KEY + T212_API_SECRET (or T212_<NAME>_API_KEY/SECRET per account), "
                    "or trading212_statement.json",
    }
    from .connections import load_all  # accounts added in Settings ("Add an account")

    conns = [c.get("label") or c["id"] for c in load_all()]
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
    out["_demo"] = demo()
    return out


def save_upload(kind: str, content: bytes) -> Path:
    """Persist an uploaded statement to data/ (kind = key from EXPECTED).
    On free hosting the disk is ephemeral (re-upload after a redeploy), but it
    takes effect instantly — no dashboard fiddling."""
    fname = dict(EXPECTED).get(kind)
    if not fname:
        raise ValueError(f"unknown upload kind: {kind}")
    return save_file(fname, content)


# What "remove this statement" deletes, per upload slot. A Trade Republic
# upload fills two files (trades, and dividends and interest).
_REMOVES = {"degiro": ("degiro_account.csv",),
            "trade_republic": ("trades_trade_republic.json", "income_trade_republic.json"),
            "tr_income": ("income_trade_republic.json",),
            "t212_statement": ("trading212_statement.json",),
            "trading212": ("trading212_statement.json",)}


def remove_upload(kind: str) -> list[str]:
    """Delete an uploaded statement from data/, so its account no longer
    counts. Returns the names removed (none when there was nothing to
    remove). Keys and accounts added in Settings are not touched."""
    names = _REMOVES.get(kind)
    if not names:
        raise ValueError(f"unknown upload kind: {kind}")
    gone = []
    for name in names:
        path = DATA_DIR / name
        if path.is_file():
            path.unlink()
            gone.append(name)
    refresh()
    return gone


def save_file(fname: str, content: bytes) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / fname
    write_atomic(dest, content)
    refresh()
    return dest


# ------------------------------------------------------- safe reads and writes
class DataFileError(ValueError):
    """One of the user's data files exists but cannot be read (damaged, cut
    off half-way, or not the shape the app writes). The message says which
    file and what to do; main.py sends it to the browser as the card's error."""

    def __init__(self, path: Path, why: str):
        self.path = Path(path)
        super().__init__(
            f"Your file {self.path.name} could not be read ({why}). Nothing was changed. "
            f"Restore it from a backup, or move it out of the data folder to start that part again.")


def write_atomic(path: Path, content: bytes | str) -> None:
    """Write `path` so a reader (or a crash, or a full disk) never sees half a
    file: write a temporary file next to it, then rename it over the old one."""
    path = Path(path)
    data = content.encode() if isinstance(content, str) else content
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)   # keep the old file's permissions
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_json(path: Path, default, lists: tuple[str, ...] = ()):
    """The JSON in `path`, or a copy of `default` when there is no file.
    Raises DataFileError when the file is there but damaged: not JSON, not
    the same kind of value as `default` (an object, a list), or one of the
    keys in `lists` is not a list of objects. Callers then show an error
    instead of an empty (zero) value, and never save over the damaged file."""
    import copy
    import json

    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return copy.deepcopy(default)
    except OSError as e:
        raise DataFileError(path, f"it cannot be opened: {e.strerror or type(e).__name__}") from None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise DataFileError(path, "it is empty or cut off" if len(raw.strip()) < 2 or raw.strip()[-1:] not in b"}]"
                            else "it is not valid JSON") from None
    if not isinstance(data, type(default)):
        raise DataFileError(path, f"expected {'an object' if isinstance(default, dict) else 'a list'}, "
                                  f"found {type(data).__name__}")
    for key in lists:
        if key not in data:
            continue
        rows = data[key]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise DataFileError(path, f'"{key}" is not a list of entries')
    return data
