"""One small in-process cache for everything slow (Yahoo, broker APIs, parsing).

Use `cached(ttl=..., stale=...)` on a function, or `get(key, fn, ...)` inline.

  ttl    seconds a value is fresh: returned straight away, no work.
  stale  extra seconds an expired value may still be served at once while one
         background thread recomputes it (stale-while-revalidate). After
         ttl + stale the caller waits for a fresh value.
  data   True (default) for anything derived from the user's own data
         (statements, cash, finances, broker connections). Such values are
         tagged with `data_version()`; when that changes they are recomputed
         before being served, so numbers never lag behind an upload or edit.
         Market data (quotes, index levels, headlines) passes data=False and
         survives uploads.
  copy   True returns a deep copy, so a caller that edits the result cannot
         corrupt the cached value.

The data version changes when `invalidate()` is called (main.py does this
after every successful POST/PUT/PATCH/DELETE under /api/, so new write
endpoints are covered without extra code) or when one of the user's data
files changes on disk (checked by mtime at most once a second).

Concurrent callers of the same key share one computation, and when a
recompute raises, the last good value is returned if there is one.
"""

from __future__ import annotations

import copy as _copy
import functools
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("stock_overview.cache")

_lock = threading.Lock()
_entries: dict[str, "_Entry"] = {}
_counter = 0                      # bumped by invalidate()
_fp: tuple[float, int] = (float("-inf"), 0)  # (checked_at, data-file fingerprint)
_FP_EVERY = 1.0                   # seconds between data-file mtime checks

# Last time a person (not a keep-alive ping) asked for data; the background
# warmer only refreshes while someone is actually using the app.
last_activity = 0.0


class _Entry:
    __slots__ = ("value", "at", "version", "has_value", "lock", "refreshing")

    def __init__(self) -> None:
        self.value: Any = None
        self.at = 0.0
        self.version: Any = None
        self.has_value = False
        self.lock = threading.RLock()   # single-flight per key
        self.refreshing = False


# ----------------------------------------------------------------- versions
def _data_files() -> list[str]:
    """Names of the user's data files (statements, cash, finances…). Anything
    bundled in the vault counts, so a new connector that adds its file to
    `vault.FILES` is watched automatically."""
    from . import datafiles, vault

    return sorted({name for _, name in datafiles.EXPECTED} | set(vault.FILES))


def _fingerprint() -> int:
    from . import datafiles

    parts = []
    for name in _data_files():
        try:
            path, real = datafiles.resolve(name)
            st = path.stat()
            parts.append((str(path), real, st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((name, None))
    return hash(tuple(parts))


def data_version() -> tuple[int, int]:
    """Changes whenever the user's data may have changed."""
    global _fp
    now = time.monotonic()
    checked_at, fp = _fp
    if now - checked_at >= _FP_EVERY:
        try:
            fp = _fingerprint()
        except Exception:  # never let the safety net break a request
            pass
        _fp = (now, fp)
    return (_counter, fp)


def invalidate() -> None:
    """Mark every data-derived value out of date (call after any write)."""
    global _counter, _fp
    with _lock:
        _counter += 1
        _fp = (float("-inf"), _fp[1])  # re-read file mtimes on the next lookup


def clear() -> None:
    """Forget everything (tests)."""
    global _fp
    with _lock:
        _entries.clear()
        _fp = (float("-inf"), 0)


def touch() -> None:
    """Record that someone is using the app (see `last_activity`)."""
    global last_activity
    last_activity = time.time()


def after_request(method: str, path: str, status: int) -> None:
    """Called by main.py for every request. A successful write under /api/
    (upload, cash, finances, watchlist, or any endpoint added later) marks
    data-derived values out of date; successful API reads count as activity.
    Keep-alive pings (/healthz) are neither."""
    if not path.startswith("/api/") or status >= 400:
        return
    touch()
    if method not in ("GET", "HEAD", "OPTIONS"):
        invalidate()


# -------------------------------------------------------------------- core
def _entry(key: str) -> _Entry:
    e = _entries.get(key)
    if e is None:
        with _lock:
            e = _entries.setdefault(key, _Entry())
    return e


def _out(value: Any, copy: bool) -> Any:
    return _copy.deepcopy(value) if copy else value


def _store(e: _Entry, value: Any, version: Any) -> None:
    e.value, e.at, e.version, e.has_value = value, time.monotonic(), version, True


def _refresh_async(key: str, e: _Entry, fn: Callable[[], Any], data: bool) -> None:
    with _lock:
        if e.refreshing:
            return
        e.refreshing = True

    def run() -> None:
        try:
            with e.lock:
                version = data_version() if data else None
                _store(e, fn(), version)
        except Exception as exc:  # keep serving the last good value
            log.info("background refresh of %s failed: %r", key, exc)
        finally:
            e.refreshing = False

    threading.Thread(target=run, name=f"refresh:{key[:40]}", daemon=True).start()


def get(key: str, fn: Callable[[], Any], ttl: float, stale: float = 0.0,
        data: bool = True, copy: bool = False) -> Any:
    """The cached value of `fn()` under `key` (see the module docstring)."""
    e = _entry(key)
    version = data_version() if data else None
    if e.has_value and e.version == version:
        value, age = e.value, time.monotonic() - e.at
        if age < ttl:
            return _out(value, copy)
        if age < ttl + stale:
            _refresh_async(key, e, fn, data)
            return _out(value, copy)

    with e.lock:  # one computation per key; others wait and reuse it
        version = data_version() if data else None
        if e.has_value and e.version == version and time.monotonic() - e.at < ttl:
            return _out(e.value, copy)
        try:
            value = fn()
        except Exception:
            if e.has_value:
                log.info("recompute of %s failed; serving the last good value", key)
                return _out(e.value, copy)
            raise
        _store(e, value, version)
        return _out(value, copy)


def peek(key: str) -> tuple[bool, float | None]:
    """(has a value, its age in seconds) — for status displays and tests."""
    e = _entries.get(key)
    if e is None or not e.has_value:
        return False, None
    return True, time.monotonic() - e.at


def cached(ttl: float, stale: float = 0.0, data: bool = True, copy: bool = False,
           key: str | None = None):
    """Decorator form of `get`. The key is the function's dotted name plus its
    arguments, so different arguments are cached separately."""
    def decorator(fn):
        base = key or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            k = base
            if args or kwargs:
                k += repr((args, sorted(kwargs.items())))
            return get(k, lambda: fn(*args, **kwargs), ttl, stale, data, copy)

        wrapper.uncached = fn  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ------------------------------------------------------------------ warmer
def start_warmer(tasks: Callable[[], list[Callable[[], Any]]], every: float = 240.0,
                 active_within: float = 900.0, first: threading.Event | None = None) -> None:
    """Run the warm-up tasks once now, in a background thread, then again every
    `every` seconds while someone used the app in the last `active_within`
    seconds. Each task goes through the caches, so fresh values cost nothing.
    `first` is set once the first task (the main dashboard) has run."""
    def run_all() -> None:
        for i, task in enumerate(tasks()):
            try:
                task()
            except Exception as exc:
                log.info("warm-up task %s failed: %r", getattr(task, "__name__", task), exc)
            if i == 0 and first is not None:
                first.set()
        if first is not None:
            first.set()

    def loop() -> None:
        run_all()
        while True:
            time.sleep(every)
            if time.time() - last_activity < active_within:
                run_all()

    threading.Thread(target=loop, name="cache-warmer", daemon=True).start()
