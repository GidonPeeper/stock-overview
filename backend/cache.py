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
recompute raises, the last good value is returned if there is one. A failure
is shared too: for a few seconds (`_ERROR_HOLD`, at most the ttl) callers get
the same error (or the last good value) at once instead of each waiting for
another attempt at a source that is down.

Memory stays bounded: keys of one family (the key up to ':' or '(', e.g. one
entry per set of tickers) beyond `_FAMILY_MAX` are dropped once nobody has
read them for `_IDLE` seconds. `trim()` does the same for small dict caches.
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

_ERROR_HOLD = 10.0                # seconds a failed computation's error is shared
_FAMILY_MAX = 8                   # entries per key family kept even when idle
_IDLE = 15 * 60                   # an entry beyond that, unread this long, is dropped

# Last time a person (not a keep-alive ping) asked for data; the background
# warmer only refreshes while someone is actually using the app.
last_activity = 0.0


class _Entry:
    __slots__ = ("value", "at", "version", "has_value", "lock", "refreshing", "error", "used")

    def __init__(self) -> None:
        self.value: Any = None
        self.at = 0.0
        self.version: Any = None
        self.has_value = False
        self.lock = threading.RLock()   # single-flight per key
        self.refreshing = False
        self.error: tuple | None = None  # (exception, version, monotonic time) of the last failure
        self.used = time.monotonic()


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
def _family(key: str) -> str:
    return key.split(":", 1)[0].split("(", 1)[0]


def _evict(family: str, now: float) -> None:
    """Drop idle entries of `family` beyond _FAMILY_MAX (call with _lock held)."""
    same = sorted((e.used, k) for k, e in _entries.items() if _family(k) == family)
    for used, k in same[:max(len(same) - _FAMILY_MAX, 0)]:
        if now - used > _IDLE:
            _entries.pop(k, None)


def _entry(key: str) -> _Entry:
    e = _entries.get(key)
    if e is None:
        with _lock:
            e = _entries.get(key)
            if e is None:
                _evict(_family(key), time.monotonic())
                e = _entries[key] = _Entry()
    e.used = time.monotonic()
    return e


def trim(d: dict, limit: int) -> None:
    """Drop the oldest items of a plain dict used as a cache, beyond `limit`."""
    while len(d) > limit:
        try:
            d.pop(next(iter(d)), None)
        except (RuntimeError, StopIteration):   # changed by another thread meanwhile
            return


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
                e.error = None
        except Exception as exc:  # keep serving the last good value
            e.error = (exc, version, time.monotonic())
            log.warning("background refresh of %s failed (serving the last good value): %r", key, exc)
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
        err = e.error
        if err is not None and err[1] == version and time.monotonic() - err[2] < min(ttl, _ERROR_HOLD):
            # it failed a moment ago (maybe while this caller waited): no new attempt yet
            if e.has_value:
                return _out(e.value, copy)
            raise err[0].with_traceback(None)
        try:
            value = fn()
        except Exception as exc:
            e.error = (exc, version, time.monotonic())
            if e.has_value:
                log.warning("recompute of %s failed (%r); serving the last good value", key, exc)
                return _out(e.value, copy)
            log.warning("computing %s failed: %r", key, exc)
            raise
        e.error = None
        _store(e, value, version)
        return _out(value, copy)


def last_error(key: str) -> BaseException | None:
    """The error of the latest attempt to compute `key` when it failed (the
    value served is then an older one), else None."""
    e = _entries.get(key)
    return e.error[0] if e is not None and e.error is not None else None


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
                 active_within: float = 900.0, idle_every: float = 1200.0,
                 first: threading.Event | None = None) -> None:
    """Run the warm-up tasks once now, in a background thread, then again every
    `every` seconds while someone used the app in the last `active_within`
    seconds, and every `idle_every` seconds otherwise (so a visit after a quiet
    spell still finds everything ready). Each task goes through the caches, so
    fresh values cost nothing. `first` is set once the first task (the main
    dashboard) has run."""
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
        last = time.time()
        while True:
            time.sleep(every)
            now = time.time()
            if now - last_activity < active_within or now - last >= idle_every:
                run_all()
                last = time.time()

    threading.Thread(target=loop, name="cache-warmer", daemon=True).start()
