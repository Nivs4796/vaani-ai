"""Chat history: every turn of every chat, kept per user next to the accounts in data/vaani.db.

web._remember() calls record() after each turn ("new", "chat", "confirm", "cancel", "action").
Two tables: chats (one row per chat session) and chat_items (one row per turn). Each item's
request and response are one JSON blob, Fernet-encrypted with VAANI_TOKEN_KEY when a key is
set and plain JSON when not (single mode never needs a key), so a missing key never loses a
turn. Titles are built in code from the first typed text; no model call. A chat with no
chat/confirm/cancel/action item (only opened) stays out of the lists. Delete is soft.
Thumbs up / down go to logs/feedback.jsonl.

Postgres swap point: these functions are the only readers and writers of the two tables.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

from assistant import memory, store

ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_LOG = ROOT / "logs" / "feedback.jsonl"

TITLE_MAX = 48  # auto titles, code-built
RENAME_MAX = 80  # titles a user types
EXCERPT_MAX = 200
LIST_MAX = 50
SHOWN_KINDS = ("chat", "confirm", "cancel", "action")  # a chat with none of these is only "opened"
UNTITLED = "Untitled chat"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    user_key TEXT NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_key, id)
);
CREATE INDEX IF NOT EXISTS chats_updated ON chats(user_key, deleted, updated_at);
CREATE TABLE IF NOT EXISTS chat_items (
    user_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ts TEXT NOT NULL,
    blob BLOB NOT NULL,
    PRIMARY KEY (user_key, chat_id, seq)
);
"""

_lock = threading.Lock()  # one writer at a time; SQLite's own lock is the backstop across processes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _key(user_id: str | None) -> str:
    return str(user_id) if user_id else "single"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Same file as store.py (read at call time, so tests can point store.DB_FILE elsewhere)."""
    path = Path(store.DB_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- blobs


def _seal(item: dict) -> bytes:
    """JSON, encrypted when VAANI_TOKEN_KEY is usable; plain JSON (starts with "{") when not."""
    text = json.dumps(item, ensure_ascii=False, default=str)
    try:
        return store.encrypt(text)
    except store.StoreError:
        return text.encode("utf-8")


def _open(blob) -> dict:
    """Never raises: a blob this key cannot read comes back as an empty, flagged item."""
    raw = bytes(blob or b"")
    try:
        text = raw.decode("utf-8") if raw[:1] == b"{" else store.decrypt(raw)
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (store.StoreError, ValueError, UnicodeDecodeError):
        pass
    return {"request": {}, "response": {}, "unreadable": True}


# --------------------------------------------------------------------------- titles


def make_title(text) -> str:
    """The first typed text on one line, at most TITLE_MAX chars, cut on a word boundary."""
    t = " ".join(str(text or "").split())
    if len(t) <= TITLE_MAX:
        return t
    room = TITLE_MAX - 1  # leave space for the ellipsis
    head = t[: room + 1]
    cut = head.rfind(" ")
    head = head[:cut] if cut > 0 else t[:room]
    return head.rstrip(" ,.;:-–—") + "…"


def _clean_title(title) -> str:
    t = " ".join(str(title or "").split())
    return t[:RENAME_MAX].rstrip()


# --------------------------------------------------------------------------- writes


def record(user_id, session_id, kind, request, response) -> None:
    """Store one turn: upsert the chat row, append the item. The first "chat" text names the chat."""
    if not session_id:
        return
    key, sid, now = _key(user_id), str(session_id), _now()
    kind = str(kind or "chat")[:20]
    req = request if isinstance(request, dict) else {}
    blob = _seal({"request": req, "response": response if isinstance(response, dict) else {}})
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO chats (user_key, id, title, created_at, updated_at) VALUES (?, ?, '', ?, ?) "
            "ON CONFLICT(user_key, id) DO UPDATE SET updated_at = excluded.updated_at",
            (key, sid, now, now),
        )
        if kind == "chat":
            title = make_title(req.get("text"))
            if title:  # never overwrites a rename or an earlier title
                conn.execute("UPDATE chats SET title = ? WHERE user_key = ? AND id = ? AND title = ''", (title, key, sid))
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_items WHERE user_key = ? AND chat_id = ?", (key, sid)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chat_items (user_key, chat_id, seq, kind, ts, blob) VALUES (?, ?, ?, ?, ?, ?)",
            (key, sid, seq, kind, now, blob),
        )


def rename(user_id, chat_id, title) -> dict | None:
    """New title (spaces collapsed, at most RENAME_MAX chars). None if the chat is not this user's.
    Raises ValueError on an empty title."""
    t = _clean_title(title)
    if not t:
        raise ValueError("title is empty")
    with _lock, _connect() as conn:
        n = conn.execute(
            "UPDATE chats SET title = ? WHERE user_key = ? AND id = ? AND deleted = 0", (t, _key(user_id), str(chat_id))
        ).rowcount
    return {"id": str(chat_id), "title": t} if n else None


def delete(user_id, chat_id) -> bool:
    """Soft delete: the chat leaves every list and GET; its rows stay (encrypted when a key is set)."""
    with _lock, _connect() as conn:
        return conn.execute(
            "UPDATE chats SET deleted = 1 WHERE user_key = ? AND id = ? AND deleted = 0", (_key(user_id), str(chat_id))
        ).rowcount > 0


def feedback(user_id, session_id, value, excerpt="") -> dict:
    """Append one thumbs up / down to logs/feedback.jsonl. Raises ValueError on any other value."""
    if value not in ("up", "down"):
        raise ValueError('value must be "up" or "down"')
    row = {"ts": _now(), "user": _key(user_id), "session_id": str(session_id or "")[:64], "value": value,
           "excerpt": " ".join(str(excerpt or "").split())[:EXCERPT_MAX]}
    path = Path(FEEDBACK_LOG)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


# --------------------------------------------------------------------------- reads


def _tz(user_id) -> ZoneInfo:
    try:
        return ZoneInfo(memory.load(user_id).get("timezone") or "UTC")
    except Exception:  # unknown zone or unreadable memory: still group, in UTC
        return ZoneInfo("UTC")


def _when(iso: str) -> datetime:
    try:
        d = datetime.fromisoformat(str(iso))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def list_chats(user_id, live: Iterable[str] = (), now: datetime | None = None) -> dict:
    """{"groups": [{"label": "Today"|"Yesterday"|"Earlier", "chats": [{id, title, updated_at, live}]}]}:
    newest first, at most LIST_MAX chats, days in the user's time zone, empty groups left out."""
    live = set(live or ())
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM chats c WHERE user_key = ? AND deleted = 0 AND EXISTS ("
            f"SELECT 1 FROM chat_items i WHERE i.user_key = c.user_key AND i.chat_id = c.id AND i.kind IN ({','.join('?' * len(SHOWN_KINDS))})"
            ") ORDER BY updated_at DESC, (SELECT MAX(rowid) FROM chat_items j WHERE j.user_key = c.user_key AND j.chat_id = c.id) DESC LIMIT ?",
            (_key(user_id), *SHOWN_KINDS, LIST_MAX),
        ).fetchall()
    tz = _tz(user_id)
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    groups = {"Today": [], "Yesterday": [], "Earlier": []}
    for r in rows:
        day = _when(r["updated_at"]).astimezone(tz).date()
        label = "Today" if day == today else "Yesterday" if day == today - timedelta(days=1) else "Earlier"
        groups[label].append({"id": r["id"], "title": r["title"] or UNTITLED, "updated_at": r["updated_at"],
                              "live": r["id"] in live})
    return {"groups": [{"label": k, "chats": v} for k, v in groups.items() if v]}


def get_chat(user_id, chat_id, live: Iterable[str] = ()) -> dict | None:
    """{"id", "title", "live", "items": [{"kind", "request", "response", "ts"}]} or None (missing,
    deleted, or another user's)."""
    key, cid = _key(user_id), str(chat_id)
    with _lock, _connect() as conn:
        chat = conn.execute("SELECT id, title FROM chats WHERE user_key = ? AND id = ? AND deleted = 0", (key, cid)).fetchone()
        if chat is None:
            return None
        rows = conn.execute(
            "SELECT kind, ts, blob FROM chat_items WHERE user_key = ? AND chat_id = ? ORDER BY seq", (key, cid)
        ).fetchall()
    items = []
    for r in rows:
        data = _open(r["blob"])
        item = {"kind": r["kind"], "request": data.get("request") or {}, "response": data.get("response") or {}, "ts": r["ts"]}
        if data.get("unreadable"):
            item["unreadable"] = True
        items.append(item)
    return {"id": chat["id"], "title": chat["title"] or UNTITLED, "live": chat["id"] in set(live or ()), "items": items}
