"""Durable user memory: name, timezone, contacts, facts, profile, autonomy prefs.

Single-user mode (no signed-in user) reads and writes data/memory.json exactly as
before. A signed-in user (context.current_user, or an explicit user_id) gets
data/users/<user_id>/memory.json instead.

Postgres swap point: load()/save() are the only readers and writers.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Callable

from assistant import context

ROOT = Path(__file__).resolve().parent.parent
MEMORY_FILE = ROOT / "data" / "memory.json"  # single-user mode
USERS_DIR = ROOT / "data" / "users"  # one folder per signed-in user

WORKFLOWS = ("replies", "scheduling", "follow_ups", "triage")

DEFAULT: dict = {
    "name": "Alex",
    "timezone": "Asia/Kolkata",
    "contacts": [
        {"name": "Andy Chen", "email": "andy.chen@example.test"},
        {"name": "Andy Patel", "email": "andy.patel@example.test"},
    ],
    "facts": [],
    "profile": {},
    "autonomy": {wf: {"level": "suggest", "send_unlocked": False, "snoozed_at": None} for wf in WORKFLOWS},
    "brief": {"days": 3},
    "followups": {"wait_days": 3, "dismissed": []},
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _resolve(user_id: str | None) -> str | None:
    return user_id if user_id is not None else context.user_id()


def path_for(user_id: str | None = None) -> Path:
    """The memory file for this user (None: the current user, else data/memory.json)."""
    uid = _resolve(user_id)
    if uid is None:
        return MEMORY_FILE
    if not _SAFE_ID.match(str(uid)):  # ids are uuid4 from store.py; anything else is a bug, never a path
        raise ValueError(f"invalid user id {uid!r}")
    return USERS_DIR / str(uid) / "memory.json"


def _lock(user_id: str | None) -> threading.RLock:
    # Re-entrant so an update() callback may call load() or save() for the same user.
    key = _resolve(user_id) or ""
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def _fill(data: dict, default: dict) -> None:
    """Add missing keys from default, recursing into nested dicts (old files lack the new ones)."""
    for key, value in default.items():
        if key not in data or (isinstance(value, dict) and not isinstance(data[key], dict)):
            data[key] = copy.deepcopy(value)
        elif isinstance(value, dict):
            _fill(data[key], value)


def load_path(path: Path) -> dict:
    """Read one memory file; fall back to DEFAULT (and fill missing keys) if it is
    missing or unreadable, so a corrupt file never takes the assistant down."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("memory.json is not an object")
    except (OSError, ValueError):
        data = copy.deepcopy(DEFAULT)
    _fill(data, DEFAULT)
    if not isinstance(data.get("contacts"), list):
        data["contacts"] = []
    if not isinstance(data.get("facts"), list):
        data["facts"] = []
    if not isinstance(data["followups"].get("dismissed"), list):
        data["followups"]["dismissed"] = []
    return data


def load(user_id: str | None = None) -> dict:
    return load_path(path_for(user_id))


def save(data: dict, user_id: str | None = None) -> None:
    """Atomic write: temp file in the same directory, fsync, os.replace."""
    path = path_for(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".memory-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(fn: Callable[[dict], None], user_id: str | None = None) -> dict:
    """Load, let fn mutate the dict in place, save, return it; all under the user's lock."""
    uid = _resolve(user_id)
    with _lock(uid):
        data = load(uid)
        fn(data)
        save(data, uid)
        return data


def append_fact(fact: str) -> dict:
    """Append one fact (whitespace-normalised, de-duplicated) and persist."""
    fact = " ".join(str(fact or "").split())
    uid = context.user_id()
    with _lock(uid):
        data = load(uid)
        if fact and fact not in data["facts"]:
            data["facts"].append(fact)
            save(data, uid)
        return data


def add_contact(name: str, email: str) -> tuple[dict, str]:
    """Add or update one contact and persist. Returns (data, "added" | "updated" | "unchanged").

    Same address -> update the name; same full name (case-insensitive) -> update the address.
    """
    name = " ".join(str(name or "").split())
    email = str(email or "").strip().lower()
    uid = context.user_id()
    with _lock(uid):
        data = load(uid)
        contacts = data["contacts"]
        for c in contacts:
            if isinstance(c, dict) and str(c.get("email", "")).lower() == email:
                if c.get("name") == name:
                    return data, "unchanged"
                c["name"] = name
                save(data, uid)
                return data, "updated"
        for c in contacts:
            if isinstance(c, dict) and str(c.get("name", "")).lower() == name.lower():
                c["email"] = email
                save(data, uid)
                return data, "updated"
        contacts.append({"name": name, "email": email})
        save(data, uid)
        return data, "added"


def render_memory_block(data: dict) -> str:
    """The memory lines that go into the system prompt."""
    lines = [f"Timezone: {data.get('timezone') or 'UTC'}."]
    contacts = [
        f"{c.get('name')} <{c.get('email')}>"
        for c in data.get("contacts", [])
        if isinstance(c, dict) and c.get("name") and c.get("email")
    ]
    lines.append("Known contacts: " + (", ".join(contacts) if contacts else "none") + ".")
    facts = [str(f) for f in data.get("facts", []) if f]
    if facts:
        lines.append("Remembered facts:")
        lines.extend(f"- {f}" for f in facts)
    else:
        lines.append("Remembered facts: none yet.")
    return "\n".join(lines)
