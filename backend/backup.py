"""Download a backup of your data, and restore one (Settings → Back up and restore).

A backup is a plain zip file you keep yourself:

    stock-overview-backup.json   what is inside: the app, its version, when it
                                 was made, and each file's size and SHA-256
    degiro_account.csv, ...      your data files, side by side (no folders)

It holds every file in vault.FILES that exists (statements, balances, budget,
watchlist, accounts added in Settings with their keys, your choices in
Settings) and two lookups worth keeping (CACHES). Never the sample files,
.env, the encrypted cloud copy (vault.enc) or bank links in progress. It is
not encrypted: the page says to keep it private.

A restore checks the whole file before it touches anything:

  * a zip of at most MAX_ZIP bytes with at most MAX_ENTRIES entries;
  * the manifest is there and lists only known file names: no folders, no
    "..", no absolute paths, no duplicates, no unknown names, nothing encrypted;
  * each file is at most MAX_FILE bytes (read with that limit, so a "zip bomb"
    stops there) and matches its size and checksum in the manifest;
  * each JSON file is an object, with lists where the app expects lists; the
    statement CSV is UTF-8 with the statement's columns.

Then it writes the files into a temporary folder next to the data, moves the
current files into data/before-restore-<date-time>/ (the safety copy), and
moves the new ones in (renames on the same disk). If a step fails, the old
files are moved back: never half a restore. Afterwards the caches are cleared
and, on a host, the cloud copy is saved again (github_sync).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import threading
import time
import zipfile
import zlib
from pathlib import Path

from . import appstate, datafiles, vault

MANIFEST = "stock-overview-backup.json"
FORMAT = 1
CACHES = ["tickers_found.json", "sectors.json"]      # lookups that take a while to rebuild
MAX_ZIP = 60_000_000
MAX_FILE = 30_000_000
MAX_TOTAL = 120_000_000
MAX_ENTRIES = 40
KEEP_PREFIX = "before-restore-"

# What each file is, in plain words (shown before a restore).
LABELS = {
    "degiro_account.csv": "DEGIRO statement",
    "trades_trade_republic.json": "Trade Republic trades",
    "income_trade_republic.json": "Trade Republic dividends and interest",
    "trading212_statement.json": "Trading 212 statement",
    "cash_accounts.json": "Bank, savings and pension balances",
    "watchlist.json": "Watchlist",
    "finances.json": "Loans, income, payments and things you own",
    "connections.json": "Connected accounts, with their keys",
    "manual_holdings.json": "Positions read from files",
    "settings.json": "Your choices in Settings (country, currency, colours)",
    "tickers_found.json": "Tickers the app looked up",
    "sectors.json": "Sectors of your holdings",
}
# JSON keys that must be lists of entries when present (as datafiles.read_json checks).
_LISTS = {"trades_trade_republic.json": ("trades",), "income_trade_republic.json": ("income",),
          "cash_accounts.json": ("accounts",), "connections.json": ("connections",),
          "watchlist.json": ("tickers",), "finances.json": ("loans", "income", "expenses", "assets"),
          "trading212_statement.json": ("trades", "positions", "dividends", "transactions")}

_lock = threading.Lock()


class BackupError(ValueError):
    """A backup that cannot be used, with what to do (shown as the message)."""


def names() -> list[str]:
    """Every file a backup may hold."""
    return list(dict.fromkeys(list(vault.FILES) + CACHES))


def _data_dir() -> Path:
    return datafiles.DATA_DIR


def _current(name: str) -> Path | None:
    """The user's own copy of `name`, wherever the app reads it from."""
    if name in CACHES:
        p = _data_dir() / name
        return p if p.is_file() else None
    path, real = datafiles.resolve(name)
    return path if real and path.is_file() else None


# ----------------------------------------------------------------- download
def make() -> tuple[bytes, list[str]]:
    """(zip bytes, names inside). Raises BackupError when there is nothing to save."""
    from . import version
    files: dict[str, bytes] = {}
    for name in names():
        p = _current(name)
        if p is not None:
            try:
                files[name] = p.read_bytes()
            except OSError:
                continue
    if not any(n not in CACHES for n in files):
        raise BackupError("There is nothing of yours to back up yet: the dashboard shows example data. "
                          "Add an account or a balance first.")
    v = version.info()
    manifest = {"app": "stock-overview", "format": FORMAT, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "version": v.get("short"), "version_date": v.get("date"),
                "files": {n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in files.items()}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST, json.dumps(manifest, indent=1))
        for n, b in files.items():
            zf.writestr(n, b)
    return buf.getvalue(), sorted(files)


def mark_downloaded() -> None:
    appstate.put("backup_downloaded_at", time.strftime("%Y-%m-%dT%H:%M:%S"))


def info() -> dict:
    """For the card: when a backup was last downloaded, and what one would hold now."""
    return {"last_downloaded": appstate.get("backup_downloaded_at"),
            "files": [{"name": n, "label": LABELS.get(n, n)} for n in names() if _current(n) is not None
                      and n not in CACHES]}


# ------------------------------------------------------------------ checking
def _check_content(name: str, data: bytes) -> None:
    if name.endswith(".json"):
        try:
            value = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise BackupError(f"{name} in the backup is damaged (not valid JSON). Nothing was changed.") from None
        if not isinstance(value, dict):
            raise BackupError(f"{name} in the backup is not what the app writes. Nothing was changed.")
        for key in _LISTS.get(name, ()):
            rows = value.get(key, [])
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise BackupError(f"{name} in the backup is damaged (\"{key}\" is not a list of entries). "
                                  "Nothing was changed.")
    else:   # the DEGIRO statement CSV
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise BackupError(f"{name} in the backup is not readable text. Nothing was changed.") from None
        head = next(csv.reader(io.StringIO(text)), [])
        if len(head) < 12:
            raise BackupError(f"{name} in the backup is not a DEGIRO account statement. Nothing was changed.")


def read(blob: bytes) -> tuple[dict, dict[str, bytes]]:
    """Check a backup completely; (manifest, {name: content}). Raises
    BackupError with the reason and what to do; nothing is written."""
    if len(blob) > MAX_ZIP:
        raise BackupError(f"That file is larger than {MAX_ZIP // 1_000_000} MB, more than a backup of this app "
                          "should be. Choose the .zip file made with “Download a backup”.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        entries = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise BackupError("That is not a backup of this app (not a zip file). Choose the .zip file made with "
                          "“Download a backup”.") from None
    known = set(names())
    with zf:
        if len(entries) > MAX_ENTRIES:
            raise BackupError("That zip holds too many files to be a backup of this app. Nothing was changed.")
        seen: set[str] = set()
        for e in entries:
            n = e.filename
            if n in seen:
                raise BackupError(f"The backup holds {n} twice. Nothing was changed.")
            seen.add(n)
            if "/" in n or "\\" in n or n.startswith(".") or ":" in n or e.is_dir():
                raise BackupError(f"The backup holds a file in a folder ({n[:60]}). A backup of this app has no "
                                  "folders. Nothing was changed.")
            if n != MANIFEST and n not in known:
                raise BackupError(f"The backup holds a file the app does not know ({n[:60]}). Nothing was changed.")
            if e.flag_bits & 0x1:
                raise BackupError("The backup is password-protected. Use the file as downloaded. Nothing was changed.")
            if e.file_size > MAX_FILE:
                raise BackupError(f"{n} in the backup is too large. Nothing was changed.")
        if MANIFEST not in seen:
            raise BackupError("That zip is not a backup of this app (stock-overview-backup.json is missing). "
                              "Choose the .zip file made with “Download a backup”.")
        total = 0
        content: dict[str, bytes] = {}
        for e in entries:
            try:
                with zf.open(e) as fh:
                    data = fh.read(MAX_FILE + 1)
            except (zipfile.BadZipFile, OSError, EOFError, zlib.error):
                raise BackupError(f"{e.filename} in the backup is damaged. Nothing was changed.") from None
            if len(data) > MAX_FILE:
                raise BackupError(f"{e.filename} in the backup is too large. Nothing was changed.")
            total += len(data)
            if total > MAX_TOTAL:
                raise BackupError("The backup unpacks to more than the app ever saves. Nothing was changed.")
            content[e.filename] = data
    try:
        manifest = json.loads(content.pop(MANIFEST).decode("utf-8"))
        listed = manifest["files"]
        fmt = int(manifest.get("format") or 0)
        if manifest.get("app") != "stock-overview" or not isinstance(listed, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        raise BackupError("That zip is not a backup of this app (its stock-overview-backup.json is not right). "
                          "Nothing was changed.") from None
    if fmt > FORMAT:
        raise BackupError("This backup was made by a newer version of the app. Update the app first "
                          "(Settings → Version and updates), then restore it.")
    if set(listed) != set(content):
        raise BackupError("The backup is incomplete: its list of files does not match what is inside. "
                          "Nothing was changed.")
    for n, data in content.items():
        want = listed[n] if isinstance(listed[n], dict) else {}
        if want.get("size") != len(data) or want.get("sha256") != hashlib.sha256(data).hexdigest():
            raise BackupError(f"{n} in the backup is damaged (it does not match its checksum). Nothing was changed.")
        _check_content(n, data)
    if not content:
        raise BackupError("That backup holds no data files. Nothing was changed.")
    return manifest, content


def preview(blob: bytes) -> dict:
    """What a restore would do, without doing it."""
    manifest, content = read(blob)
    files = [{"name": n, "label": LABELS.get(n, n), "size": len(content[n]),
              "replaces": _current(n) is not None} for n in names() if n in content]
    gone = [{"name": n, "label": LABELS.get(n, n)} for n in names()
            if n not in content and _current(n) is not None and n not in CACHES]
    return {"created": manifest.get("created"), "version": manifest.get("version"),
            "version_date": manifest.get("version_date"), "files": files, "set_aside": gone}


# ------------------------------------------------------------------ restoring
def restore(blob: bytes) -> dict:
    """Check, then replace the data files with the backup's (see the module
    docstring). Raises BackupError; nothing changes unless all went well."""
    manifest, content = read(blob)
    data_dir = _data_dir()
    with _lock:
        data_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        stage = data_dir / f".restoring-{stamp}-{os.getpid()}"
        keep = data_dir / f"{KEEP_PREFIX}{stamp}"
        n = 1
        while keep.exists():
            n += 1
            keep = data_dir / f"{KEEP_PREFIX}{stamp}-{n}"
        stage.mkdir()
        try:
            for name, data in content.items():
                datafiles.write_atomic(stage / name, data)
            keep.mkdir()
            aside: list[str] = []
            placed: list[str] = []
            try:
                for name in names():
                    cur = data_dir / name
                    if cur.is_file():
                        os.replace(cur, keep / name)
                        aside.append(name)
                for name in content:
                    os.replace(stage / name, data_dir / name)
                    placed.append(name)
            except BaseException as e:
                for name in placed:                      # put everything back as it was
                    try:
                        (data_dir / name).unlink()
                    except OSError:
                        pass
                for name in aside:
                    try:
                        os.replace(keep / name, data_dir / name)
                    except OSError:
                        pass
                raise BackupError(f"The restore could not be finished ({getattr(e, 'strerror', None) or type(e).__name__}). "
                                  "Your files were put back as they were. Check that the disk is not full and "
                                  "try again.") from None
        except OSError as e:
            raise BackupError(f"The backup could not be unpacked ({e.strerror or type(e).__name__}). Nothing was "
                              "changed. Check that the disk is not full and the data folder can be written to.") from None
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        if not aside:
            try:
                keep.rmdir()
            except OSError:
                pass
    after_restore()
    return {"restored": sorted(content), "created": manifest.get("created"),
            "safety_copy": f"data/{keep.name}" if aside else None, "set_aside": sorted(set(aside) - set(content))}


def after_restore() -> None:
    """Forget everything read from the old files, and save the cloud copy anew."""
    from . import cache, connections, github_sync, instruments, prefs, sectors, sources
    datafiles.refresh()
    cache.invalidate()
    with connections._lock:
        connections._read_cache.clear()
        connections._cache.clear()
        connections._failed.clear()
        connections.damaged.clear()
    prefs._cache[0] = None
    instruments._found = None
    sectors._memory = None
    with sources._plock:
        sources._last_good.clear()
        sources._problems.clear()
    github_sync.push_vault_async()      # on a host: the cloud copy now holds the restored data
