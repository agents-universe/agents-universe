"""AES-256-GCM token encryption/decryption.

Key derivation: PBKDF2(secret_key, salt=user_id_bytes, iterations=100_000)
Never logs or returns plaintext tokens in error messages.
"""
from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from api.config import get_settings

_SETTINGS = get_settings()
_ITERATIONS = 100_000
_KEY_LEN = 32  # 256 bits


@lru_cache(maxsize=1024)
def _derive_key(user_id: str) -> bytes:
    # PBKDF2 with 100k iterations takes 50-150ms per derivation and
    # runs synchronously inside the event loop. A single turn derives keys
    # multiple times per user (model config + integrations) — cache the
    # derived key (secret_key + user_id are stable) instead of re-deriving.
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        _SETTINGS.secret_key.encode(),
        user_id.encode(),
        _ITERATIONS,
        dklen=_KEY_LEN,
    )
    return dk


def encrypt(plaintext: str, user_id: str) -> str:
    """Encrypt plaintext and return base64-encoded 'nonce:ciphertext'."""
    key = _derive_key(user_id)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    blob = base64.b64encode(nonce + ct).decode()
    return blob


def decrypt(ciphertext: str, user_id: str) -> str:
    """Decrypt base64-encoded ciphertext. Raises on failure."""
    key = _derive_key(user_id)
    aesgcm = AESGCM(key)
    blob = base64.b64decode(ciphertext)
    nonce = blob[:12]
    ct = blob[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()


def decrypt_or_none(ciphertext: str, user_id: str) -> str | None:
    """Decrypt, returning None instead of raising on corrupted data.

    Corrupted ciphertext (manual DB edits, key rotation, truncation) raises
    binascii.Error / InvalidTag; REST routes calling decrypt directly would
    surface a 500. Returns None so callers can answer 400 with a clear
    message ("delete and re-save").
    """
    try:
        return decrypt(ciphertext, user_id)
    except Exception:
        return None


def key_hint(plaintext: str) -> str:
    """Return last 4 characters of plaintext for display purposes."""
    if len(plaintext) <= 4:
        return "****"
    return "..." + plaintext[-4:]


# ---------------------------------------------------------------------------
# Project-scoped encryption: secret belongs to a project, not a user.
# Any user with project access can decrypt via server-side tools.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1024)
def _derive_project_key(project_id: str) -> bytes:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        _SETTINGS.secret_key.encode(),
        f"project:{project_id}".encode(),
        _ITERATIONS,
        dklen=_KEY_LEN,
    )
    return dk


def encrypt_project_secret(plaintext: str, project_id: str) -> str:
    """Encrypt plaintext scoped to a project. Any project member can decrypt."""
    key = _derive_project_key(project_id)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt_project_secret(ciphertext: str, project_id: str) -> str:
    """Decrypt project-scoped ciphertext. Raises on failure."""
    key = _derive_project_key(project_id)
    aesgcm = AESGCM(key)
    blob = base64.b64decode(ciphertext)
    nonce = blob[:12]
    ct = blob[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()


def decrypt_project_secret_or_none(ciphertext: str, project_id: str) -> str | None:
    """Project-scoped variant of decrypt_or_none."""
    try:
        return decrypt_project_secret(ciphertext, project_id)
    except Exception:
        return None
