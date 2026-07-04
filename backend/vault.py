"""Encrypted data vault — ships private statement files inside a public repo.

The problem: the repo is public (no plaintext personal data allowed), and free
hosting has an ephemeral disk (uploads vanish whenever the service restarts).
The fix: the private data files are encrypted into `data/vault.enc` (committed —
it's ciphertext) and decrypted on boot with a key derived from an env secret
that already lives on the host (DATA_KEY if set, else DASHBOARD_SECRET). The
key never enters the repository.

Re-encrypt after updating statements:  .venv/bin/python -m backend.vault
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
VAULT = DATA_DIR / "vault.enc"

# Private files bundled when present.
FILES = ["degiro_account.csv", "trades_trade_republic.json",
         "income_trade_republic.json", "cash_accounts.json"]

_ITERATIONS = 600_000


def _key_material() -> str | None:
    return os.getenv("DATA_KEY") or os.getenv("DASHBOARD_SECRET")


def _fernet(salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    material = _key_material()
    if not material:
        return None
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=_ITERATIONS)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(material.encode())))


def encrypt() -> list[str]:
    """Bundle every present private file into the vault. Run locally."""
    salt = os.urandom(16)
    f = _fernet(salt)
    if f is None:
        raise SystemExit("Set DATA_KEY or DASHBOARD_SECRET in .env first.")
    bundled = {}
    for name in FILES:
        p = DATA_DIR / name
        if p.exists():
            bundled[name] = base64.b64encode(f.encrypt(p.read_bytes())).decode()
    if not bundled:
        raise SystemExit("No private data files found to encrypt.")
    VAULT.write_text(json.dumps({
        "salt": base64.b64encode(salt).decode(),
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "files": bundled,
    }))
    return sorted(bundled)


def unlock() -> dict:
    """Decrypt vault contents into data/ on boot. Existing real files win.
    Never raises — a wrong key just means the app stays on sample data."""
    result = {"present": VAULT.exists(), "unlocked": [], "skipped": [], "error": None}
    if not VAULT.exists():
        return result
    try:
        blob = json.loads(VAULT.read_text())
        f = _fernet(base64.b64decode(blob["salt"]))
        if f is None:
            result["error"] = "no key material (DATA_KEY / DASHBOARD_SECRET unset)"
            return result
        DATA_DIR.mkdir(exist_ok=True)
        for name, enc in blob.get("files", {}).items():
            if name not in FILES:
                continue
            dest = DATA_DIR / name
            if dest.exists():
                result["skipped"].append(name)   # a newer upload/local file wins
                continue
            dest.write_bytes(f.decrypt(base64.b64decode(enc)))
            result["unlocked"].append(name)
    except Exception as e:  # wrong key, corrupt file — never break the app
        result["error"] = type(e).__name__
    return result


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass
    names = encrypt()
    print(f"vault.enc written with {len(names)} file(s): {', '.join(names)}")
