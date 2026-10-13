"""Durable user memory in data/memory.json: name, timezone, contacts, facts.

Postgres swap point: load()/save() are the only readers and writers.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY_FILE = ROOT / "data" / "memory.json"

DEFAULT: dict = {
    "name": "Alex",
    "timezone": "Asia/Kolkata",
    "contacts": [
        {"name": "Andy Chen", "email": "andy.chen@example.test"},
        {"name": "Andy Patel", "email": "andy.patel@example.test"},
    ],
    "facts": [],
}


def load() -> dict:
    """Read memory.json; fall back to DEFAULT (and fill missing keys) if it is
    missing or unreadable, so a corrupt file never takes the assistant down."""
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("memory.json is not an object")
    except (OSError, ValueError):
        data = copy.deepcopy(DEFAULT)
    for key, value in DEFAULT.items():
        data.setdefault(key, copy.deepcopy(value))
    if not isinstance(data.get("contacts"), list):
        data["contacts"] = []
    if not isinstance(data.get("facts"), list):
        data["facts"] = []
    return data


def save(data: dict) -> None:
    """Atomic write: temp file in the same directory, fsync, os.replace."""
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=MEMORY_FILE.parent, prefix=".memory-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MEMORY_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_fact(fact: str) -> dict:
    """Append one fact (whitespace-normalised, de-duplicated) and persist."""
    fact = " ".join(str(fact or "").split())
    data = load()
    if fact and fact not in data["facts"]:
        data["facts"].append(fact)
        save(data)
    return data


def add_contact(name: str, email: str) -> tuple[dict, str]:
    """Add or update one contact and persist. Returns (data, "added" | "updated" | "unchanged").

    Same address -> update the name; same full name (case-insensitive) -> update the address.
    """
    name = " ".join(str(name or "").split())
    email = str(email or "").strip().lower()
    data = load()
    contacts = data["contacts"]
    for c in contacts:
        if isinstance(c, dict) and str(c.get("email", "")).lower() == email:
            if c.get("name") == name:
                return data, "unchanged"
            c["name"] = name
            save(data)
            return data, "updated"
    for c in contacts:
        if isinstance(c, dict) and str(c.get("name", "")).lower() == name.lower():
            c["email"] = email
            save(data)
            return data, "updated"
    contacts.append({"name": name, "email": email})
    save(data)
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
