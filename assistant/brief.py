"""The inbox brief that opens every chat: a stamp plus five groups, built in code.

build() reads the inbox through tools.list_recent_emails (one listing and one batched
Gmail request), sorts the rows into Needs a reply / Follow-ups / Worth knowing /
Updates / Promotions and returns both a dict for the page and a markdown version for
the transcript. No model call. The result is cached per user for VAANI_BRIEF_TTL_MIN
minutes so opening a new chat does not hit Gmail every time.

The markdown deliberately repeats agent.format_inbox_brief's voice instead of importing
agent.py, which will import this module.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from assistant import context, followups, memory, tools

MAX_RESULTS = 20
GROUP_KEYS = ("needs_reply", "follow_ups", "worth_knowing", "updates", "promotions")
TITLES = {
    "needs_reply": "Needs a reply",
    "follow_ups": "Follow-ups",
    "worth_knowing": "Worth knowing",
    "updates": "Updates",
    "promotions": "Promotions",
}
LIMITS = {"needs_reply": 8, "follow_ups": 5, "worth_knowing": 5, "updates": 3, "promotions": 0}
ACTIONS = {"needs_reply": ["reply"], "follow_ups": ["nudge", "dismiss"], "worth_knowing": ["open"], "updates": [], "promotions": []}
# An update is worth knowing when Gmail marks it important or it is about money, access or a meeting.
WORTH_KNOWING_RE = re.compile(
    r"(?i)\b(?:invites?|invited|invitation|meetings?|bills?|billing|invoices?|receipts?|payments?|statements?|security|"
    r"sign[\s-]?in|password|action\s+required|deadline|due|overdue)\b"
)

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers


def _tidy(text: str) -> str:
    """Collapse whitespace and the stray space some senders put before ")"."""
    return re.sub(r"\s+\)", ")", " ".join(str(text or "").split()))


def _clean_subject(sender: str, subject: str) -> str:
    subj = _tidy(subject) or "(no subject)"
    # Drop a "Sender:" prefix ("Basecamp (X): Here's the latest" -> "Here's the latest"), but only
    # when a colon follows, so "Cloudflare Connect is almost sold out" keeps its first word.
    if sender and subj.lower().startswith(sender.lower()):
        rest = subj[len(sender):]
        if rest.lstrip().startswith(":") and rest.lstrip(" :").strip():
            subj = rest.lstrip(" :")
    return subj if len(subj) <= 90 else subj[:87].rstrip() + "..."


def _int(value, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _memory(uid: str | None) -> dict:
    try:
        return memory.load(uid) if uid else memory.load()
    except Exception:
        return {}


def _tz(mem: dict) -> ZoneInfo:
    try:
        return ZoneInfo(mem.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _days(mem: dict) -> int:
    """VAANI_BRIEF_DAYS when set (a demo-day override), else memory brief.days, else 3 (clamped to 1..30).

    memory's DEFAULT always fills brief.days, so the env var has to win or it would never apply."""
    days = os.environ.get("VAANI_BRIEF_DAYS", "").strip() or None
    if days is None:
        days = (mem.get("brief") or {}).get("days") if isinstance(mem.get("brief"), dict) else None
    if days is None or str(days).strip() == "":
        days = "3"
    return max(1, min(_int(days, 3), 30))


def _ttl_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("VAANI_BRIEF_TTL_MIN", "10"))) * 60
    except ValueError:
        return 600.0


def _followups_on() -> bool:
    return os.environ.get("VAANI_FOLLOWUPS", "0").strip().lower() in {"1", "true", "yes", "on"}


def _span(days: int) -> str:
    return "last day" if days == 1 else f"last {days} days"


@contextlib.contextmanager
def _as_user(uid: str | None):
    """tools.* finds the user through context; make an explicit user_id win for this build."""
    if uid is None or uid == context.user_id():
        yield
        return
    token = context.current_user.set({"id": uid})
    try:
        yield
    finally:
        context.current_user.reset(token)


def _entries(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """Same sender + subject collapse into one entry (newest first, as Gmail returned them)."""
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        sender = _tidy(r.get("sender") or r.get("from") or "?")
        key = (sender, _clean_subject(sender, r.get("subject", "")))
        e = out.get(key)
        if e is None:
            e = out[key] = {"row": r, "n": 0, "unread": False, "important": False, "ids": []}
        e["n"] += 1
        e["unread"] = e["unread"] or bool(r.get("unread"))
        e["important"] = e["important"] or bool(r.get("important"))
        e["ids"].append(r.get("id"))
    return out


def _out_row(key: tuple[str, str], e: dict, group: str) -> dict:
    r = e["row"]
    row = {
        "id": r.get("id"),
        "sender": key[0],
        "subject": key[1],
        "date": r.get("date", ""),
        "unread": e["unread"],
        "snippet": r.get("snippet", ""),
        "n": e["n"],
        "actions": list(ACTIONS[group]),
    }
    tid = r.get("thread_id") or r.get("threadId")
    if tid:
        row["thread_id"] = tid
    return row


def _group(key: str, entries: list[tuple[tuple[str, str], dict]], empty_text: str) -> dict:
    return {
        "key": key,
        "title": TITLES[key],
        "count": len(entries),
        "messages": sum(e["n"] for _k, e in entries),
        "rows": [_out_row(k, e, key) for k, e in entries[: LIMITS[key]]],
        "empty_text": empty_text,
    }


def _followup_group(uid: str | None) -> dict:
    if not _followups_on():
        return {"key": "follow_ups", "title": TITLES["follow_ups"], "count": 0, "messages": 0, "rows": [], "empty_text": "Follow-ups are off"}
    rows = []
    for c in followups.candidates(user_id=uid, limit=LIMITS["follow_ups"]):
        row = dict(c)
        row.update({"id": c.get("message_id"), "unread": False, "n": 1, "actions": list(ACTIONS["follow_ups"])})
        rows.append(row)
    return {"key": "follow_ups", "title": TITLES["follow_ups"], "count": len(rows), "messages": len(rows), "rows": rows,
            "empty_text": "No follow-ups waiting"}


def _groups(data: dict, days: int, uid: str | None) -> list[dict]:
    rows = data.get("messages") or []
    needs = _entries([r for r in rows if r.get("group") == "needs_reply"])
    updates = _entries([r for r in rows if r.get("group", "updates") == "updates"])
    promos = _entries([r for r in rows if r.get("group") == "promotions"])
    worth, rest = [], []
    for k, e in updates.items():
        if k in needs:
            continue  # already listed under Needs a reply
        if e["important"] or WORTH_KNOWING_RE.search(f"{k[0]} {k[1]}"):
            worth.append((k, e))
        else:
            rest.append((k, e))
    promo = _group("promotions", list(promos.items()), "No promotions")
    promo["action"] = "archive_all"
    return [
        _group("needs_reply", list(needs.items()), f"Nothing from a person in the {_span(days)}."),
        _followup_group(uid),
        _group("worth_knowing", worth, "Nothing important in the updates"),
        _group("updates", rest, "No other updates"),
        promo,
    ]


def _lines(g: dict, with_date: bool = False, with_wait: bool = False) -> list[str]:
    lines = []
    for i, r in enumerate(g["rows"], 1):
        extra = []
        if r.get("n", 1) > 1:
            extra.append(f"x{r['n']}")
        if r.get("unread"):
            extra.append("unread")
        if with_wait and r.get("days_waiting") is not None:
            d = r["days_waiting"]
            extra.append(f"waiting {d} day{'s' if d != 1 else ''}")
        if with_date and r.get("date"):
            extra.append(r["date"])
        lines.append(f"{i}. {r.get('sender', '?')} - {r.get('subject', '(no subject)')}" + (f" ({', '.join(extra)})" if extra else ""))
    hidden = g["count"] - len(g["rows"])
    if hidden > 0:
        lines.append(f"... and {hidden} more")
    return lines


def _text(data: dict, groups: list[dict], days: int) -> str:
    rows = data.get("messages") or []
    by_key = {g["key"]: g for g in groups}
    if not rows and not by_key["follow_ups"]["rows"]:
        return f"Nothing in your inbox from the {_span(days)}."
    unread = f"{data.get('unread_count', 0)}{'+' if data.get('unread_count_note') == '100+' else ''}"
    head = f"Inbox, {_span(days)}: {unread} unread."
    if data.get("more_available"):
        head += f" Showing the newest {len(rows)}."
    parts = [head]
    needs = by_key["needs_reply"]
    parts.append(f"**{needs['title']}**\n" + ("\n".join(_lines(needs, with_date=True)) if needs["rows"] else needs["empty_text"]))
    fu = by_key["follow_ups"]
    if fu["rows"]:
        parts.append(f"**{fu['title']}**\n" + "\n".join(_lines(fu, with_wait=True)))
    for key in ("worth_knowing", "updates"):
        g = by_key[key]
        if g["rows"]:
            parts.append(f"**{g['title']}**\n" + "\n".join(_lines(g)))
    promo = by_key["promotions"]
    if promo["messages"]:
        n = promo["messages"]
        parts.append(f"**{promo['title']}**\n{n} promotional email{'s' if n != 1 else ''}, nothing that needs you.")
    first_person = needs["rows"][0]["sender"].split()[0] if needs["rows"] else ""
    parts.append(f'Ask me about any of these, for example "What does {first_person} want?"' if first_person else "Ask me about any of these.")
    return "\n\n".join(parts)


def _error_brief(message: str, days: int, tz: ZoneInfo, raw: str = "") -> dict:
    return {
        "error": message,
        "built_at": datetime.now(tz).isoformat(timespec="seconds"),
        "covers_days": days,
        "count": 0,
        "unread": 0,
        "groups": [],
        "text": f"I couldn't read your inbox just now: {message} Nothing was changed; try again in a moment.",
        "tool_result": raw,
    }


def _build(uid: str | None) -> dict:
    mem = _memory(uid)
    days, tz = _days(mem), _tz(mem)
    try:
        with _as_user(uid):
            raw = tools.list_recent_emails(max_results=MAX_RESULTS, query=f"in:inbox newer_than:{days}d")
            if not isinstance(raw, str) or raw.lstrip().startswith("ERROR:"):
                return _error_brief(str(raw).strip()[:300], days, tz, raw if isinstance(raw, str) else "")
            data = json.loads(raw)
            groups = _groups(data, days, uid)
        return {
            "built_at": datetime.now(tz).isoformat(timespec="seconds"),
            "covers_days": days,
            "count": _int(data.get("count"), len(data.get("messages") or [])),
            "unread": _int(data.get("unread_count"), 0),
            "groups": groups,
            "text": _text(data, groups, days),
            "tool_result": raw,
        }
    except Exception as e:
        return _error_brief(f"{type(e).__name__}: {e}", days, tz)


# --------------------------------------------------------------------------- public


def build(user_id: str | None = None, refresh: bool = False) -> dict:
    """The brief for this user, from cache when younger than VAANI_BRIEF_TTL_MIN. Never raises.
    Errors are returned as {"error": ..., "groups": [], ...} and never cached."""
    try:
        uid = user_id or context.user_id()
        key = uid or "_single"
        now = time.monotonic()
        if not refresh:
            with _cache_lock:
                hit = _cache.get(key)
            if hit and now - hit[0] < _ttl_seconds():
                return copy.deepcopy(hit[1])
        out = _build(uid)
        if "error" not in out:
            with _cache_lock:
                _cache[key] = (now, copy.deepcopy(out))
        return out
    except Exception as e:  # the last backstop: the first message of a chat must never crash
        return _error_brief(f"{type(e).__name__}: {e}", 3, ZoneInfo("UTC"))


def invalidate(user_id: str | None = None) -> None:
    """Drop this user's cached brief (after archiving, dismissing, or sending from it)."""
    key = (user_id or context.user_id()) or "_single"
    with _cache_lock:
        _cache.pop(key, None)
