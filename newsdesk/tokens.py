"""Stores the refreshed Instagram token encrypted, so the state file can live in a public GitHub repo.

The key is derived from the IG_ACCESS_TOKEN secret (which only GitHub Secrets / .env hold). If that secret is
replaced, old stored tokens simply can't be decrypted and the new secret is used instead.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet(seed: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("newsdesk:" + seed).encode()).digest()))


def save(db, token: str, seed: str | None) -> None:
    if not seed:
        return
    db.set("ig_token_enc", _fernet(seed).encrypt(token.encode()).decode())


def load(db, seed: str | None) -> str | None:
    blob = db.get("ig_token_enc")
    if not blob or not seed:
        return None
    try:
        return _fernet(seed).decrypt(blob.encode()).decode()
    except InvalidToken:
        return None
