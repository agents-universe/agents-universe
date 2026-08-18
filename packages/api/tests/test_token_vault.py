"""Unit tests for AES-256-GCM token vault — pure crypto, no DB / Redis / network."""
from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from api.services import token_vault


def test_encrypt_decrypt_roundtrip():
    blob = token_vault.encrypt("ghp_secret-token-123", "user-1")
    assert blob != "ghp_secret-token-123"
    assert token_vault.decrypt(blob, "user-1") == "ghp_secret-token-123"


def test_encrypt_produces_random_ciphertext_per_call():
    plaintext = "same-token"
    assert token_vault.encrypt(plaintext, "user-1") != token_vault.encrypt(plaintext, "user-1")


def test_decrypt_with_wrong_user_fails():
    blob = token_vault.encrypt("token", "user-1")
    with pytest.raises(InvalidTag):
        token_vault.decrypt(blob, "user-2")


def test_decrypt_with_tampered_ciphertext_fails():
    blob = bytearray(token_vault.encrypt("token", "user-1"), "utf-8")
    blob[-1] = b"A"[0] if blob[-1] != b"A"[0] else b"B"[0]
    with pytest.raises((InvalidTag, ValueError)):
        token_vault.decrypt(blob.decode(), "user-1")


def test_project_secret_roundtrip():
    blob = token_vault.encrypt_project_secret("proj-secret", "project-42")
    assert token_vault.decrypt_project_secret(blob, "project-42") == "proj-secret"


def test_project_secret_wrong_project_fails():
    blob = token_vault.encrypt_project_secret("proj-secret", "project-42")
    with pytest.raises(InvalidTag):
        token_vault.decrypt_project_secret(blob, "project-43")


def test_key_hint_masks_short_values():
    assert token_vault.key_hint("ab") == "****"
    assert token_vault.key_hint("ghp_1234abcd") == "...abcd"
