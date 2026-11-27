"""Small facts the app keeps about itself: when a backup was last downloaded,
and the last answer of the check for a newer version.

Kept in data/app_state.json (git-ignored). It is not part of the backup or
the cloud copy: it describes this copy of the app, not your money. A missing
or damaged file simply means "nothing known yet".
"""

from __future__ import annotations

import json
import threading

from . import datafiles

FILE = "app_state.json"
_lock = threading.Lock()


def _path():
    return datafiles.DATA_DIR / FILE


def load() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default=None):
    return load().get(key, default)


def put(key: str, value) -> None:
    """Save one value (never raises: this is bookkeeping, not your data)."""
    with _lock:
        data = load()
        data[key] = value
        try:
            datafiles.DATA_DIR.mkdir(exist_ok=True)
            datafiles.write_atomic(_path(), json.dumps(data, indent=1))
        except OSError:
            pass
