"""Follow-up detector: threads where the user wrote last to a real person and has been
waiting a few days for an answer. Pure code, read-only against Gmail, no model call.

candidates() lists the user's sent mail from the last 14 days, reads each thread's
headers in one batched request and keeps a thread only when the user's message is
still the newest one, went to at least one person (not a list, a no-reply address or
the user), was not bulk or auto-submitted, and has not been dismissed. Rows that look
like a question come first, then the longest wait.
"""

from __future__ import annotations

import email.utils
import html
import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from assistant import context, google_auth, memory

LOOKBACK_DAYS = 14
SENT_MAX = 50
BATCH_MAX = 50  # Gmail asks for at most 50 calls per batch request
SNIPPET_MAX = 140
THREAD_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", "List-Unsubscribe", "Auto-Submitted"]
AUTOMATED_ADDR_RE = re.compile(r"(?i)(no-?reply|do-?not-?reply|notifications?|mailer-daemon|bounce)")
# Role and list mailboxes are not a person who owes the user an answer.
LIST_LOCAL_RE = re.compile(r"(?i)^(?:lists?|all|everyone|announce(?:ments?)?|digest|newsletters?|news|updates|alerts?|notify)$")
LIST_DOMAIN_RE = re.compile(r"(?i)(?:^|\.)(?:googlegroups\.com|groups\.[^.]+\..+|lists\.[^.]+\..+)$")
EXPECTS_REPLY_RE = re.compile(
    r"(?i)\?|\b(?:let me know|could you|can you|would you|please confirm|please advise|please let me|thoughts|"
    r"waiting|following up|follow up on|any update|get back to me)\b"
)
SUBJECT_PREFIX_RE = re.compile(r"(?i)^\s*(?:re|fwd?|aw|sv)\s*(?:\[\d+\])?\s*:\s*")

_own_cache: dict[str, str] = {}
_own_lock = threading.Lock()


def _uid(user_id: str | None) -> str | None:
    return user_id or context.user_id()


def _memory(uid: str | None) -> dict:
    try:
        return memory.load(uid) if uid else memory.load()
    except Exception:
        return {}


def _gmail(uid: str | None):
    services = google_auth.get_services(uid) if uid else google_auth.get_services()
    return services["gmail"]


def _own_address(uid: str | None) -> str:
    """The signed-in user's address: the session user when it matches, else Gmail's profile."""
    key = uid or "_single"
    with _own_lock:
        if key in _own_cache:
            return _own_cache[key]
    u = context.current_user.get() or {}
    addr = ""
    if u.get("email") and (uid is None or u.get("id") == uid):
        addr = str(u["email"])
    else:
        try:
            addr = google_auth.connected_email(uid) if uid else google_auth.connected_email()
        except Exception:
            addr = ""
    addr = addr.strip().lower()
    if addr:
        with _own_lock:
            _own_cache[key] = addr
    return addr


def _tz(mem: dict) -> ZoneInfo:
    try:
        return ZoneInfo(mem.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _int(value, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def wait_days_for(mem: dict, wait_days=None) -> int:
    """Explicit argument, else VAANI_FOLLOWUP_DAYS when set (a demo-day override), else memory
    followups.wait_days, else 3. memory's DEFAULT always fills wait_days, so the env var goes first."""
    if wait_days is None or str(wait_days).strip() == "":
        wait_days = os.environ.get("VAANI_FOLLOWUP_DAYS", "").strip() or None
    if wait_days is None:
        wait_days = (mem.get("followups") or {}).get("wait_days") if isinstance(mem.get("followups"), dict) else None
    if wait_days is None:
        wait_days = "3"
    # older_than must stay inside the 14-day lookback or the query matches nothing.
    return max(1, min(_int(wait_days, 3), LOOKBACK_DAYS - 1))


def _headers(msg: dict) -> dict:
    out = {}
    for h in (msg.get("payload") or {}).get("headers") or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:
            out[name] = h.get("value") or ""
    return out


def _batch_threads(gmail, ids: list[str]) -> dict[str, dict]:
    """threads.get for many ids in one HTTP round trip per 50 (same style as tools._batch_get).
    Items that fail inside the batch are retried one by one with num_retries=2."""
    out: dict[str, dict] = {}
    failed: list[str] = []

    def collect(request_id, response, exception):
        if exception is None and response is not None:
            out[request_id] = response
        else:
            failed.append(request_id)

    def one(tid: str):
        return gmail.users().threads().get(userId="me", id=tid, format="metadata", metadataHeaders=THREAD_HEADERS)

    for i in range(0, len(ids), BATCH_MAX):
        chunk = ids[i : i + BATCH_MAX]
        batch = gmail.new_batch_http_request(callback=collect)
        for tid in chunk:
            batch.add(one(tid), request_id=tid)
        try:
            batch.execute()
        except Exception:
            failed.extend(t for t in chunk if t not in out and t not in failed)
    for tid in failed:
        try:
            out[tid] = one(tid).execute(num_retries=2)
        except Exception:
            pass  # a thread we still cannot read is skipped, never fatal
    return out


def _is_person(addr: str, own: str) -> bool:
    addr = (addr or "").strip().lower()
    if "@" not in addr or addr == own:
        return False
    local, _, domain = addr.rpartition("@")
    if AUTOMATED_ADDR_RE.search(local) or LIST_LOCAL_RE.match(local) or LIST_DOMAIN_RE.search(domain):
        return False
    return True


def _clean_subject(subject: str) -> str:
    s = " ".join(str(subject or "").split())
    while True:
        stripped = SUBJECT_PREFIX_RE.sub("", s, count=1)
        if stripped == s:
            return s
        s = stripped


def _when(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz)


def _row_for_thread(thread: dict, own: str, now: datetime, tz: ZoneInfo, wait: int) -> dict | None:
    msgs = [m for m in thread.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
    if not msgs:
        return None
    msgs.sort(key=lambda m: _int(m.get("internalDate"), 0))
    last = msgs[-1]
    hdr = _headers(last)
    _disp, frm = email.utils.parseaddr(hdr.get("from", ""))
    # The user must have written last: anyone else after them means the thread was answered.
    if not own or frm.strip().lower() != own:
        return None
    if hdr.get("list-unsubscribe") or hdr.get("auto-submitted", "no").strip().lower() not in {"", "no"}:
        return None
    people = [(n, a.strip().lower()) for n, a in email.utils.getaddresses([hdr.get("to", ""), hdr.get("cc", "")]) if _is_person(a, own)]
    if not people:
        return None
    sent_at = _when(_int(last.get("internalDate"), 0), tz)
    days = (now - sent_at).days
    if days < wait:
        return None
    name, addr = people[0]
    snippet = html.unescape(last.get("snippet") or "")[:SNIPPET_MAX]
    expects = bool(EXPECTS_REPLY_RE.search(snippet))
    # A one-off send that asks nothing (an app notice, a receipt sent from this account) is not
    # waiting on anyone: keep a thread only if it is a conversation or the user asked for something.
    conversation = any(email.utils.parseaddr(_headers(m).get("from", ""))[1].strip().lower() not in ("", own) for m in msgs[:-1])
    if not (conversation or expects):
        return None
    to_name = " ".join((name or addr).split())
    return {
        "thread_id": thread.get("id") or last.get("threadId"),
        "message_id": last.get("id"),
        "to_name": to_name,
        "to_email": addr,
        "subject": " ".join((hdr.get("subject") or "(no subject)").split()),
        "days_waiting": days,
        "snippet": snippet,
        "expects_reply": expects,
        "sender": to_name,
        "date": sent_at.strftime("%Y-%m-%d %H:%M"),
    }


def candidates(user_id: str | None = None, wait_days=None, limit=5) -> list[dict]:
    """Threads waiting on someone else, best first. Never raises: any failure is an empty list."""
    try:
        uid = _uid(user_id)
        mem = _memory(uid)
        wait = wait_days_for(mem, wait_days)
        tz = _tz(mem)
        own = _own_address(uid)
        if not own:
            return []
        gmail = _gmail(uid)
        q = f"in:sent newer_than:{LOOKBACK_DAYS}d older_than:{wait}d"
        listing = gmail.users().messages().list(userId="me", q=q, maxResults=SENT_MAX).execute(num_retries=2)
        tids: list[str] = []
        for m in listing.get("messages") or []:
            tid = m.get("threadId")
            if tid and tid not in tids:
                tids.append(tid)
        fu = mem.get("followups") if isinstance(mem.get("followups"), dict) else {}
        dismissed = {str(t) for t in (fu.get("dismissed") or [])}
        tids = [t for t in tids if t not in dismissed]
        if not tids:
            return []
        threads = _batch_threads(gmail, tids)
        now = datetime.now(tz)
        rows = []
        for tid in tids:
            t = threads.get(tid)
            row = _row_for_thread(t, own, now, tz, wait) if t else None
            if row and row["thread_id"] not in dismissed:
                rows.append(row)
        rows.sort(key=lambda r: (not r["expects_reply"], -r["days_waiting"]))
        return rows[: max(0, _int(limit, 5))]
    except Exception:
        return []


def dismiss(thread_id: str, user_id: str | None = None) -> bool:
    """Hide one thread from follow-ups for good (memory followups.dismissed). Never raises."""
    tid = str(thread_id or "").strip()
    if not tid:
        return False
    uid = _uid(user_id)

    def add(data: dict) -> None:
        fu = data.get("followups")
        if not isinstance(fu, dict):
            fu = data["followups"] = {}
        if not isinstance(fu.get("dismissed"), list):
            fu["dismissed"] = []
        if tid not in fu["dismissed"]:
            fu["dismissed"].append(tid)

    try:
        memory.update(add, user_id=uid)
    except Exception:
        return False
    try:
        from assistant import brief  # late import: brief imports this module

        brief.invalidate(uid)
    except Exception:
        pass
    return True


def _first_name(name: str) -> str:
    name = " ".join(str(name or "").split())
    if "@" in name:
        name = re.split(r"[._\-+]", name.split("@", 1)[0])[0].capitalize()
    return name.split()[0] if name.split() else ""


def nudge_body(row: dict, user_name: str) -> str:
    """A short, plain follow-up note. Template only; the card still shows it before anything is sent."""
    first = _first_name(row.get("to_name") or row.get("sender") or row.get("to_email") or "")
    subject = _clean_subject(row.get("subject") or "")
    about = f" about {subject}" if subject and subject != "(no subject)" else ""
    ask = "Could you let me know when you get a chance?" if row.get("expects_reply") else "Let me know if you need anything from me."
    me = _first_name(user_name)
    greeting = f"Hi {first}," if first else "Hi,"
    return f"{greeting} just following up on my note{about}. {ask}\n\nThanks,\n{me}".rstrip()
