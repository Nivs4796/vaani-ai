"""Vaani accounts: SQLite at data/vaani.db (users + their encrypted Google refresh tokens).

Only used when someone signs in with Google (VAANI_AUTH=google, or the REPL's --user).
Refresh tokens are Fernet-encrypted with VAANI_TOKEN_KEY before they touch disk; run
`python -m assistant.auth init-env` once to put a key in .env.

Postgres swap point: these functions are the only readers and writers of the two tables.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = ROOT / "data" / "vaani.db"
load_dotenv(ROOT / ".env")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    google_sub TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    name TEXT,
    picture TEXT,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS users_email ON users(email);
CREATE TABLE IF NOT EXISTS google_tokens (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_enc BLOB NOT NULL,
    scopes TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class StoreError(Exception):
    """Missing or wrong VAANI_TOKEN_KEY, or an unreadable token."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """One short-lived connection per call: commit on success, roll back on error, always close."""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def _fernet() -> Fernet:
    key = os.environ.get("VAANI_TOKEN_KEY", "").strip()
    if not key:
        raise StoreError("VAANI_TOKEN_KEY is not set; run `python -m assistant.auth init-env` and restart")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise StoreError(f"VAANI_TOKEN_KEY is not a valid Fernet key ({e})") from None


def encrypt(text: str) -> bytes:
    return _fernet().encrypt(text.encode("utf-8"))


def decrypt(blob: bytes) -> str:
    try:
        return _fernet().decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken:
        raise StoreError("stored Google token cannot be decrypted with this VAANI_TOKEN_KEY; sign in again") from None


def _user(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def upsert_user(sub: str, email: str, name: str, picture: str) -> dict:
    """Create or refresh the account for one Google user and return its row.

    Matched by Google's stable `sub`. A row imported from token.json (sub "import:<email>")
    is claimed by the first real sign-in with that email, so its memory and token carry over.
    """
    email = str(email or "").strip().lower()
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE google_sub = ?", (sub,)).fetchone()
        if row is None and not str(sub).startswith("import:"):
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? AND google_sub LIKE 'import:%' ORDER BY created_at LIMIT 1", (email,)
            ).fetchone()
        if row is None:
            uid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, google_sub, email, name, picture, created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, sub, email, name, picture, now, now),
            )
        else:
            uid = row["id"]
            conn.execute(
                "UPDATE users SET google_sub = ?, email = ?, name = ?, picture = ?, last_login_at = ? WHERE id = ?",
                (sub, email, name, picture, now, uid),
            )
        return _user(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def get_user(user_id: str) -> dict | None:
    with _connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_email(email: str) -> dict | None:
    email = str(email or "").strip().lower()
    with _connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE email = ? ORDER BY created_at LIMIT 1", (email,)).fetchone())


def first_user_id() -> str | None:
    """The oldest account: the one that inherits data/memory.json."""
    with _connect() as conn:
        row = conn.execute("SELECT id FROM users ORDER BY created_at, rowid LIMIT 1").fetchone()
        return row["id"] if row else None


def save_token(user_id: str, refresh_token: str, scopes) -> None:
    if not refresh_token:
        raise StoreError("no refresh token to save")
    scopes_text = scopes if isinstance(scopes, str) else " ".join(scopes or [])
    blob = encrypt(refresh_token)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO google_tokens (user_id, refresh_token_enc, scopes, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET refresh_token_enc = excluded.refresh_token_enc, "
            "scopes = excluded.scopes, updated_at = excluded.updated_at",
            (user_id, blob, scopes_text, _now()),
        )


def get_token(user_id: str) -> dict | None:
    """{"refresh_token", "scopes": [...], "updated_at"} or None. Raises StoreError if undecryptable."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM google_tokens WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return {
        "refresh_token": decrypt(row["refresh_token_enc"]),
        "scopes": str(row["scopes"] or "").split(),
        "updated_at": row["updated_at"],
    }


def delete_token(user_id: str) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM google_tokens WHERE user_id = ?", (user_id,)).rowcount > 0
