"""The seven tools the model can call. Every tool:

- coerces its argument types (ints arrive as strings, optionals as "" or None),
- catches everything and returns "ERROR: <what>; nothing was changed; <next step>",
- returns a JSON string on success,
- is wrapped in @obs.logged so each call is one line in logs/tools.jsonl.

The only write path is create_calendar_event, which is code-gated on the
session's confirmed_draft_id (set by the server, never by the model).
"""

from __future__ import annotations

import base64
import email.header
import email.utils
import html
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from assistant import autonomy, context, google_auth, memory, obs
from assistant.obs import logged

BODY_MAX_CHARS = 4000
SNIPPET_MAX = 140
DEFAULT_DURATION = 30
MIN_DURATION, MAX_DURATION = 5, 480
EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
# Automated senders are "FYI" in the inbox brief, never "Needs a reply".
# "Ravi Mehta (Basecamp)", "Paula Sanders via Jira": a real person relayed by a service.
RELAYED_PERSON_RE = re.compile(r"^\s*(?P<person>[A-Z][\w'.\-]+(?:\s+[A-Z][\w'.\-]+)+)\s*(?:\([^)]*\)|\bvia\b.*)\s*$")
INBOX_GROUPS = ("needs_reply", "updates", "promotions")
LIST_HEADERS = ["From", "Subject", "Date", "List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted"]
BATCH_MAX = 50  # Gmail asks for at most 50 calls per batch request
AUTOMATED_FROM_RE = re.compile(r"(?i)\b(no-?reply|do-?not-?reply|notifications?|alerts?|mailer-daemon|newsletter|postmaster|bounce|noreply)\b")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_RE = re.compile(r"(?i)\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
RELATIVE_DAY_RE = re.compile(r"(?i)\b(today|tomorrow|yesterday|tonight)\b")
# "Andy means Andy Chen" (saved by remember) narrows an ambiguous first name.
ALIAS_FACT_RE = re.compile(r"(?i)^\s*(?:remember\s+)?(?:that\s+)?(.+?)\s+(?:means|refers to|is|=)\s+(.+?)\s*\.?\s*$")
# Attendee rule: an address may be added to an event only if it is a saved contact in
# memory.json or the user typed it in this chat. Addresses the model read in an email or
# made up are refused, so mail content can never put a stranger on an event. Extra
# domains can be allowed with ALLOWED_ATTENDEE_DOMAINS=a.com,b.org in .env; "*" allows all.
# Email recipients follow the same rule (manage.py), plus the sender being replied to and,
# for a follow-up nudge, the To and Cc of the user's own sent message being followed up.
ALLOWED_ATTENDEE_DOMAINS = os.environ.get("ALLOWED_ATTENDEE_DOMAINS", "").strip()

# --------------------------------------------------------------------------- helpers


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _error(what: str, next_step: str) -> str:
    return f"ERROR: {what}; nothing was changed; {next_step}"


def _int(value, default: int) -> int:
    """Coerce an int that may arrive as str/float/None/''. Falls back to default."""
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _str(value, default: str = "") -> str:
    if value is None:
        return default
    s = str(value).strip()
    return s if s else default


def _tz() -> ZoneInfo:
    name = memory.load().get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _now() -> datetime:
    return datetime.now(_tz()).replace(microsecond=0)


def _decode_header(value: str) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:
        return value or ""


def _local_date(value: str, tz: ZoneInfo) -> str:
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value or ""


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "br", "blockquote"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _strip_html(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _b64_bytes(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _part_charset(part: dict) -> str | None:
    for h in part.get("headers") or []:
        if (h.get("name") or "").lower() == "content-type":
            m = re.search(r'charset="?([\w.\-]+)"?', h.get("value") or "", re.IGNORECASE)
            if m:
                return m.group(1)
    return None


def _decode_text(raw: bytes, charset: str | None) -> str:
    """Decode with the part's declared charset, then utf-8, then latin-1."""
    for codec in (charset, "utf-8", "latin-1"):
        if not codec:
            continue
        try:
            return raw.decode(codec, "strict" if codec != "latin-1" else "replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _b64(data: str, charset: str | None = None) -> str:
    return _decode_text(_b64_bytes(data), charset)


def _walk_parts(part: dict, plain: list[str], htmls: list[str], others: list[str] | None = None) -> None:
    """Recursive MIME walk collecting decoded text/plain and text/html bodies.
    Other text/* subtypes (text/calendar, text/x-amp-html, text/csv) go to
    `others`, used only when there is no plain or html body; attachments are skipped."""
    mime = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    data = body.get("data")
    if data and mime.startswith("text/") and not part.get("filename"):
        try:
            text = _b64(data, _part_charset(part))
        except Exception:
            text = ""
        if text.strip():
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                htmls.append(text)
            elif others is not None:
                others.append(text)
    for sub in part.get("parts") or []:
        _walk_parts(sub, plain, htmls, others)


def _headers(payload: dict) -> dict:
    out = {}
    for h in payload.get("headers") or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:
            out[name] = _decode_header(h.get("value") or "")
    return out


def _fmt_google_time(d: dict | None, tz: ZoneInfo) -> tuple[str, bool]:
    """Google start/end dict -> ('2026-10-14 15:00', False) or ('2026-10-14', True)."""
    d = d or {}
    if d.get("date"):
        return d["date"], True
    raw = d.get("dateTime") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M"), False
    except Exception:
        return raw, False


def _when(start: datetime, end: datetime) -> str:
    same_day = start.date() == end.date()
    end_txt = end.strftime("%H:%M") if same_day else end.strftime("%a %d %b %Y %H:%M")
    return f"{start.strftime('%a %d %b %Y %H:%M')}-{end_txt} {start.tzname() or ''}".strip()


def _contacts_matching(name: str, data: dict) -> list[dict]:
    """Memory contacts whose name contains every token of `name` (case-insensitive)."""
    tokens = [t for t in re.split(r"[\s,]+", name.lower()) if t]
    if not tokens:
        return []
    out = []
    for c in data.get("contacts", []):
        if not isinstance(c, dict):
            continue
        cname = str(c.get("name", "")).lower()
        cemail = str(c.get("email", "")).lower()
        if all(t in cname or t == cemail for t in tokens):
            out.append({"name": c.get("name"), "email": c.get("email"), "source": "memory"})
    return out


def _alias_fact(name: str, data: dict) -> tuple[str, str] | None:
    """(target name, the fact) when a remembered fact says '<name> means <target>'."""
    q = " ".join(name.lower().split())
    for fact in data.get("facts", []) or []:
        m = ALIAS_FACT_RE.match(str(fact))
        if m and " ".join(m.group(1).lower().split()) == q:
            return m.group(2).strip(), str(fact)
    return None


def _resolve_contacts(name: str, data: dict) -> tuple[list[dict], str | None]:
    """Memory matches for `name`, narrowed by a remembered alias when ambiguous.
    Returns (candidates, fact used or None)."""
    candidates = _contacts_matching(name, data)
    if len(candidates) > 1:
        alias = _alias_fact(name, data)
        if alias:
            target, fact = alias
            narrowed = [c for c in candidates if str(c.get("name", "")).lower() == target.lower()]
            if not narrowed:
                narrowed = [c for c in _contacts_matching(target, data) if c in candidates]
            if len(narrowed) == 1:
                return narrowed, fact
    return candidates, None


_WORD_RE = re.compile(r"[a-z][a-z'\-]+")


def _latest_user_text(session) -> str:
    for m in reversed(getattr(session, "messages", []) or []):
        if m.get("role") == "user":
            return _user_text(str(m.get("content", "")))
    return ""


_SCHEDULE_INTENT_RE = re.compile(r"(?i)\b(?:call|meeting|meet|book|schedule|set\s+up|add|invite|catch[\s-]?up|sync|1:1)\b")


def ambiguous_contact_question(text: str) -> str | None:
    """A fixed "which one?" when a scheduling request names a first name shared by 2+ saved contacts."""
    if not _SCHEDULE_INTENT_RE.search(text or ""):
        return None
    amb = _ambiguous_name_in(_user_text(text), memory.load())
    if not amb:
        return None
    token, cs = amb
    names = [str(c["name"]) for c in cs]
    listed = " and ".join(names) if len(names) == 2 else ", ".join(names[:-1]) + f" or {names[-1]}"
    return f"You have {len(names)} contacts named {token.capitalize()}: {listed}. Which one should I invite?"


def _ambiguous_name_in(text: str, data: dict) -> tuple[str, list[dict]] | None:
    """A bare name in the user's request shared by two or more known contacts, when
    the request names no full contact name and no remembered alias resolves it.
    This is the code-enforced "which Andy?" rule: the 7B model sometimes drafts
    for one of them anyway after find_contact told it to ask."""
    low = " ".join(text.lower().split())
    words = set(_WORD_RE.findall(low))
    by_token: dict[str, list[dict]] = {}
    for c in data.get("contacts", []) or []:
        if isinstance(c, dict) and c.get("name") and c.get("email"):
            for t in _WORD_RE.findall(str(c["name"]).lower()):
                by_token.setdefault(t, []).append(c)
    for t, cs in by_token.items():
        if t not in words or len(cs) < 2:
            continue
        if any(str(c["name"]).lower() in low for c in cs):
            continue  # a full name is in the request
        narrowed, _fact = _resolve_contacts(t, data)
        if len(narrowed) == 1:
            continue  # a remembered alias ("Andy means Andy Chen") resolves it
        return t, cs
    return None


_ADDR_FIND_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


def _typed_addresses(session) -> set[str]:
    """Every email address the user typed in this chat (user messages only, never tool results)."""
    out: set[str] = set()
    for m in getattr(session, "messages", []) or []:
        if m.get("role") == "user":
            out.update(a.lower() for a in _ADDR_FIND_RE.findall(_user_text(str(m.get("content", "")))))
    return out


def _saved_addresses(data: dict) -> set[str]:
    return {str(c.get("email", "")).lower() for c in data.get("contacts", []) or [] if isinstance(c, dict) and c.get("email")}


def _attendee_allowed(addr: str, data: dict, typed: set[str]) -> bool:
    if ALLOWED_ATTENDEE_DOMAINS == "*" or addr in typed or addr in _saved_addresses(data):
        return True
    extra = {d.strip().lower() for d in ALLOWED_ATTENDEE_DOMAINS.split(",") if d.strip()}
    return addr.rsplit("@", 1)[-1] in extra


def _coerce_attendees(value, data: dict, typed: set[str] | None = None) -> tuple[list[str], str | None]:
    """Accept None/''/str/list; resolve bare names via memory. Returns (emails, error)."""
    if value is None or value == "" or value == []:
        return [], None
    items: list = []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = text
        if isinstance(value, str):
            items = [v for v in re.split(r"[,;\n]+", value) if v.strip()]
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, dict):
        items = [value]
    emails: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("email") or item.get("name") or ""
        raw = str(item).strip()
        if not raw:
            continue
        _disp, parsed = email.utils.parseaddr(raw)  # handles "Ravi Mehta (Basecamp) <x@y.com>"
        s = parsed if "@" in parsed else raw.strip("<>")
        s = s.removeprefix("mailto:").strip()
        if "@" in s:
            if not EMAIL_RE.match(s):
                return [], f"'{s}' is not a valid email address"
            addr = s.lower()
        else:
            matches, _fact = _resolve_contacts(s, data)
            if len(matches) != 1:
                return [], f"'{s}' is not an email address and matches {len(matches)} known contacts"
            addr = str(matches[0]["email"]).lower()
        if addr not in emails:
            emails.append(addr)
    for addr in emails:
        if not _attendee_allowed(addr, data, typed or set()):
            return [], (
                f"{addr} is not a saved contact and the user has not typed it in this chat, so it cannot be an "
                "attendee (addresses read from emails are never used on their own)"
            )
    return emails, None


def _contacts_named_in(text: str, data: dict) -> list[str]:
    """Emails of known contacts whose full name appears in `text` (case-insensitive)."""
    low = text.lower()
    out = []
    for c in data.get("contacts", []):
        if isinstance(c, dict) and c.get("name") and c.get("email") and str(c["name"]).lower() in low:
            addr = str(c["email"]).lower()
            if addr not in out:
                out.append(addr)
    return out


DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_start(start_iso: str, tz: ZoneInfo) -> datetime | None:
    s = start_iso.strip().replace("Z", "+00:00")
    if DATE_ONLY_RE.match(s):
        return None  # a bare date would silently become midnight; the caller asks for a time
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt.replace(second=0, microsecond=0)


def _add_minutes(start: datetime, minutes: int) -> datetime:
    """Real elapsed minutes (UTC arithmetic), correct across a DST change."""
    return (start.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(start.tzinfo)


def _user_text(content: str) -> str:
    return re.sub(r"^\[now:[^\]]*\]\s*", "", content or "", count=1)


def _requested_weekday(session) -> int | None:
    """Weekday index the user named in their latest request, or None.
    Looks at the last three user messages, newest first; a relative word
    (today/tomorrow/yesterday) in a newer message wins over an older weekday."""
    seen = 0
    for m in reversed(getattr(session, "messages", []) or []):
        if m.get("role") != "user":
            continue
        text = _user_text(str(m.get("content", "")))
        if text.startswith("User clicked") or text.startswith("You did not call"):
            continue
        days = {d.lower() for d in WEEKDAY_RE.findall(text)}
        if len(days) == 1:
            return WEEKDAYS.index(days.pop())
        if days or RELATIVE_DAY_RE.search(text):
            return None
        seen += 1
        if seen >= 3:
            break
    return None


def _requested_relative_day(session, today: date) -> date | None:
    """The date meant by "today"/"tonight"/"tomorrow" in the user's latest request, or None.
    Same scan as _requested_weekday: the newest user message that names a day decides,
    so "Andy Chen" answering "Add a call with Andy tomorrow" still resolves to tomorrow."""
    seen = 0
    for m in reversed(getattr(session, "messages", []) or []):
        if m.get("role") != "user":
            continue
        text = _user_text(str(m.get("content", "")))
        if text.startswith("User clicked") or text.startswith("You did not call"):
            continue
        rel = {w.lower() for w in RELATIVE_DAY_RE.findall(text)}
        if WEEKDAY_RE.search(text) or len(rel) > 1 or "yesterday" in rel:
            return None  # weekday check or the past check handles these
        if rel:
            return today + timedelta(days=1) if "tomorrow" in rel else today
        seen += 1
        if seen >= 3:
            break
    return None


def _next_date_for(weekday: int, today: date) -> date:
    """The date the [now] tag lists for that weekday: within the next seven days."""
    for i in range(1, 8):
        d = today + timedelta(days=i)
        if d.weekday() == weekday:
            return d
    return today


def _new_event_id() -> str:
    """Caller-supplied Calendar event id (base32hex, lowercase a-v 0-9), so a retried
    insert is a 409 'already exists' instead of a duplicate event."""
    return base64.b32hexencode(secrets.token_bytes(16)).decode("ascii").lower().rstrip("=")


def _http_status(e: Exception) -> int | None:
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- tools


def _batch_get(gmail, ids: list[str], headers: list[str] | None = None) -> dict[str, dict]:
    """messages.get for many ids in one HTTP round trip per 50 (Gmail batch API).
    Items that fail inside the batch are retried one by one with num_retries=2."""
    out: dict[str, dict] = {}
    failed: list[str] = []

    def collect(request_id, response, exception):
        if exception is None and response is not None:
            out[request_id] = response
        else:
            failed.append(request_id)

    def one(mid: str):
        req = gmail.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=headers) if headers \
            else gmail.users().messages().get(userId="me", id=mid, format="metadata")
        return req

    for i in range(0, len(ids), BATCH_MAX):
        chunk = ids[i : i + BATCH_MAX]
        batch = gmail.new_batch_http_request(callback=collect)
        for mid in chunk:
            batch.add(one(mid), request_id=mid)
        try:
            batch.execute()
        except Exception:
            failed.extend(m for m in chunk if m not in out and m not in failed)
    for mid in failed:
        try:
            out[mid] = one(mid).execute(num_retries=2)
        except Exception:
            pass  # a message we still cannot read is skipped, never fatal for the listing
    return out


def _mail_group(sender: str, labels: list[str], hdr: dict, own_first: str) -> str:
    """needs_reply | updates | promotions, from Gmail labels, bulk-mail headers and the sender.

    Gmail's own categories are not enough on their own: Basecamp digests land in Personal
    and some marketing in Updates. Order matters: a named person relayed by a service
    counts as a person; then promotions; then anything bulk or automated is an update.
    """
    disp, _addr = email.utils.parseaddr(sender)
    m = RELAYED_PERSON_RE.match(disp or "")
    if m:
        is_own = bool(own_first) and m.group("person").split()[0].lower() == own_first
        return "updates" if is_own else "needs_reply"
    if "CATEGORY_PROMOTIONS" in labels:
        return "promotions"
    bulk = "list-unsubscribe" in hdr or "list-id" in hdr or hdr.get("precedence", "").strip().lower() in {"bulk", "list", "junk"}
    automated_sender = bool(AUTOMATED_FROM_RE.search(sender))
    if bulk and not automated_sender and "CATEGORY_SOCIAL" not in labels and "CATEGORY_FORUMS" not in labels:
        return "promotions"
    auto_submitted = hdr.get("auto-submitted", "no").strip().lower() not in {"", "no"}
    if bulk or automated_sender or auto_submitted or any(c in labels for c in ("CATEGORY_UPDATES", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")):
        return "updates"
    return "needs_reply"


def _own_first_name() -> str:
    name = str(memory.load().get("name") or "").strip().split()
    return name[0].lower() if name else ""


@logged
def list_recent_emails(max_results=10, query="in:inbox newer_than:3d") -> str:
    """Newest inbox messages as compact rows {id, from, subject, date, unread, important, group, snippet},
    plus the true unread count for the query (not just among the rows returned)."""
    try:
        n = max(1, min(_int(max_results, 10), 25))
        q = context.gmail_scope(_str(query, "in:inbox newer_than:3d"))
        tz = _tz()
        gmail = google_auth.get_services()["gmail"]
        listing = gmail.users().messages().list(userId="me", q=q, maxResults=n).execute(num_retries=2)
        ids = [m["id"] for m in listing.get("messages", []) or []]
        if not ids:
            return _json({"query": q, "count": 0, "unread_count": 0, "messages": [], "note": "No messages matched this query."})
        unread_listing = gmail.users().messages().list(userId="me", q=f"({q}) is:unread", maxResults=100).execute(num_retries=2)
        unread_total = len(unread_listing.get("messages", []) or [])
        fetched = _batch_get(gmail, ids, LIST_HEADERS)
        own_first = _own_first_name()
        rows = []
        for mid in ids:  # keep Gmail's newest-first order
            msg = fetched.get(mid)
            if msg is None:
                continue
            hdr = _headers(msg.get("payload") or {})
            labels = msg.get("labelIds") or []
            sender = hdr.get("from", "")
            disp, addr = email.utils.parseaddr(sender)
            group = _mail_group(sender, labels, hdr, own_first)
            rows.append(
                {
                    "id": mid,
                    "from": sender,
                    "sender": " ".join((disp or addr or sender).split()),  # display name, whitespace-normalised
                    "subject": hdr.get("subject", "(no subject)"),
                    "date": _local_date(hdr.get("date", ""), tz),
                    "unread": "UNREAD" in labels,
                    "important": "IMPORTANT" in labels,  # Gmail's own importance marker
                    "group": group,
                    "kind": "person" if group == "needs_reply" else "automated",
                    "snippet": html.unescape(msg.get("snippet") or "")[:SNIPPET_MAX],
                }
            )
        out = {
            "query": q,
            "count": len(rows),
            "unread_count": unread_total,
            "unread_count_note": "100+" if unread_listing.get("nextPageToken") else "exact",
            "more_available": bool(listing.get("nextPageToken")),
            "messages": rows,
        }
        if len(rows) < len(ids):
            out["note"] = f"{len(ids) - len(rows)} message(s) could not be read and were skipped."
        return _json(out)
    except Exception as e:
        return _error(f"could not list emails ({type(e).__name__}: {e})", "check the Google connection and try again")


@logged
def read_email(message_id="") -> str:
    """Decoded body of one message (text/plain, else stripped text/html, else snippet)."""
    try:
        mid = _str(message_id)
        if not mid:
            return _error("message_id is required", "call list_recent_emails first and pass one of its ids")
        tz = _tz()
        gmail = google_auth.get_services()["gmail"]
        msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute(num_retries=2)
        payload = msg.get("payload") or {}
        hdr = _headers(payload)
        plain: list[str] = []
        htmls: list[str] = []
        others: list[str] = []
        _walk_parts(payload, plain, htmls, others)
        if plain:
            body = "\n".join(plain)
        elif htmls:
            body = _strip_html("\n".join(htmls))
        elif others:
            body = _strip_html("\n".join(others))
        else:
            body = html.unescape(msg.get("snippet") or "")
        body = "\n".join(" ".join(line.split()) for line in body.splitlines() if line.strip())
        truncated = len(body) > BODY_MAX_CHARS
        return _json(
            {
                "id": mid,
                "from": hdr.get("from", ""),
                "to": hdr.get("to", ""),
                "subject": hdr.get("subject", "(no subject)"),
                "date": _local_date(hdr.get("date", ""), tz),
                "unread": "UNREAD" in (msg.get("labelIds") or []),
                "body": body[:BODY_MAX_CHARS] + (" ...[truncated]" if truncated else ""),
            }
        )
    except Exception as e:
        return _error(f"could not read message {message_id!r} ({type(e).__name__}: {e})", "check the id from list_recent_emails and try again")


@logged
def list_calendar_events(days_ahead=7, on_date="") -> str:
    """Primary-calendar events from now until N days ahead, or for one whole day (on_date)."""
    try:
        days = max(1, min(_int(days_ahead, 7), 60))
        tz = _tz()
        now = _now()
        until = now + timedelta(days=days)
        if _str(on_date):
            from assistant.manage import _parse_day
            day = _parse_day(_str(on_date), now.date())
            if day is None:
                return _error(f"could not read on_date {on_date!r}", "pass YYYY-MM-DD, today, tomorrow or a weekday")
            now = datetime.combine(day, datetime.min.time(), tz)
            until = now + timedelta(days=1)
        cal = google_auth.get_services()["calendar"]
        resp = (
            cal.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=until.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute(num_retries=2)
        )
        rows = []
        for ev in resp.get("items", []) or []:
            if ev.get("status") == "cancelled":
                continue
            start, all_day = _fmt_google_time(ev.get("start"), tz)
            end, _ = _fmt_google_time(ev.get("end"), tz)
            if all_day:
                # Google's all-day end date is exclusive: a one-day event ends "tomorrow".
                try:
                    end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
                except ValueError:
                    pass
                when = f"{start} (all day)" if end == start else f"{start} to {end} (all day)"
            else:
                end_short = end[11:] if end[:10] == start[:10] else end
                when = f"{start} to {end_short}"
            rows.append(
                {
                    "id": ev.get("id"),
                    "summary": ev.get("summary") or "(no title)",
                    "start": start,
                    "end": end,
                    "all_day": all_day,
                    "when": when,
                    "attendees": [a.get("email") for a in ev.get("attendees", []) if a.get("email")],
                    "location": ev.get("location") or "",
                    "htmlLink": ev.get("htmlLink"),
                }
            )
        out = {
            "timezone": str(tz),
            "from": now.strftime("%Y-%m-%d %H:%M"),
            "to": until.strftime("%Y-%m-%d %H:%M"),
            "count": len(rows),
            "events": rows,
        }
        if not rows:
            out["note"] = "No events in this window."
        elif resp.get("nextPageToken"):
            out["note"] = f"Showing the first {len(rows)} events only; more exist in this window."
        return _json(out)
    except Exception as e:
        return _error(f"could not list calendar events ({type(e).__name__}: {e})", "check the Google connection and try again")


_own_email: str | None = None  # single-user mode (tests set it directly)
_own_by_user: dict[str, str] = {}


def _own_address() -> str:
    """The connected account's address, per signed-in user (one global in single-user mode)."""
    global _own_email
    uid = context.user_id()
    if uid is not None:
        if uid not in _own_by_user:
            addr = str((context.current_user.get() or {}).get("email") or "")
            if not addr:
                try:
                    addr = google_auth.connected_email()
                except Exception:
                    addr = ""
            if not addr:
                return ""  # not cached: try again next time
            _own_by_user[uid] = addr.lower()
        return _own_by_user[uid]
    if _own_email is None:
        try:
            _own_email = google_auth.connected_email().lower()
        except Exception:
            _own_email = ""
    return _own_email


@logged
def find_contact(name="") -> str:
    """Candidate email addresses for a name: memory.json first, then Gmail headers."""
    try:
        who = _str(name)
        if not who:
            return _error("name is required", "pass the person's name")
        data = memory.load()
        candidates, alias_fact = _resolve_contacts(who, data)
        source = "memory"
        if not candidates:
            source = "gmail"
            gmail = google_auth.get_services()["gmail"]
            safe = who.replace('"', "")
            listing = (
                gmail.users()
                .messages()
                .list(userId="me", q=context.gmail_scope(f'from:"{safe}" OR to:"{safe}" OR cc:"{safe}"'), maxResults=10)
                .execute(num_retries=2)
            )
            own = _own_address()
            seen: dict[str, dict] = {}
            tokens = [t for t in re.split(r"[\s,]+", who.lower()) if t]
            found_ids = [m["id"] for m in listing.get("messages", []) or []]
            fetched = _batch_get(gmail, found_ids, ["From", "To", "Cc"])
            for mid in found_ids:
                msg = fetched.get(mid)
                if msg is None:
                    continue
                values = [_decode_header(h.get("value") or "") for h in (msg.get("payload") or {}).get("headers") or []]
                for disp, addr in email.utils.getaddresses(values):
                    addr = addr.lower().strip()
                    if not addr or addr == own or not EMAIL_RE.match(addr):
                        continue
                    hay = f"{disp} {addr}".lower()
                    if all(t in hay for t in tokens) and addr not in seen:
                        seen[addr] = {"name": disp or addr.split("@")[0], "email": addr, "source": "gmail"}
            candidates = list(seen.values())[:5]
        n = len(candidates)
        if n == 1 and alias_fact:
            note = f"Exactly one match (remembered fact: {alias_fact}); use this address without asking."
        elif n == 1 and source == "gmail":
            note = ("One address found in recent mail; it is not a saved contact. Show it to the user and ask them to "
                    "type it in the chat if they want to invite it; do not draft with it until they do.")
        elif n == 1:
            note = "Exactly one match; use this address."
        elif n == 0:
            note = "No match in memory or recent mail. Ask the user for the email address."
        else:
            note = "More than one match. Ask the user which one before drafting; do not guess."
        return _json({"query": who, "source": source, "count": n, "candidates": candidates, "note": note})
    except Exception as e:
        return _error(f"could not look up {name!r} ({type(e).__name__}: {e})", "ask the user for the email address")


@logged
def draft_calendar_event(title="", start_iso="", duration_minutes=DEFAULT_DURATION, attendee_emails=None, description="") -> str:
    """Validate a proposed event and store it as a draft on the session. Never writes to Google."""
    try:
        session = obs.current_session.get()
        if session is None:
            return _error("no active session", "drafts can only be made inside a conversation turn")
        data = memory.load()
        tz = _tz()
        summary = _str(title)
        if not summary:
            return _error("title is missing", "give the event a title, e.g. 'Call with Andy Chen'")
        ambiguous = _ambiguous_name_in(_latest_user_text(session), data)
        if ambiguous:
            token, cs = ambiguous
            names = ", ".join(f"{c['name']} <{c['email']}>" for c in cs)
            return _error(
                f"'{token.capitalize()}' matches {len(cs)} known contacts ({names}) and the user has not said which",
                "ask the user one short question: which one? Do not guess and do not draft until they answer",
            )
        raw_start = _str(start_iso)
        if not raw_start:
            return _error("start_iso is missing", "pass the start as ISO 8601 local time, e.g. 2026-10-14T15:00")
        if DATE_ONLY_RE.match(raw_start):
            return _error(f"start_iso {raw_start!r} has no time of day", "ask the user what time, then pass e.g. 2026-10-14T15:00")
        start = _parse_start(raw_start, tz)
        if start is None:
            return _error(f"could not parse start_iso {raw_start!r}", "pass the start as ISO 8601 local time, e.g. 2026-10-14T15:00")
        now = _now().replace(second=0)  # compare at minute granularity, like start
        # Weekday check BEFORE the past check: "Friday 10am" mapped to today's date must come
        # back as "use Friday = <date>" (the model then retries), not "in the past" (it gives up).
        wanted = _requested_weekday(session)
        if wanted is not None and start.weekday() != wanted:
            right = _next_date_for(wanted, now.date())
            return _error(
                f"start {start:%Y-%m-%d} is a {start:%A} but the request says {WEEKDAYS[wanted].capitalize()}",
                f"use {WEEKDAYS[wanted].capitalize()} = {right.isoformat()} and draft again",
            )
        # "today"/"tomorrow" is unambiguous, so a wrong date from the model is corrected here
        # (same time of day) rather than sent back for a retry the 7B model sometimes fumbles.
        # The draft is still shown and still needs Confirm.
        date_note = None
        want_date = _requested_relative_day(session, now.date())
        if want_date is not None and start.date() != want_date:
            label = "today" if want_date == now.date() else "tomorrow"
            date_note = f"start date corrected from {start:%Y-%m-%d} to {label}, {want_date:%A} {want_date.isoformat()}, as the user asked"
            start = start.replace(year=want_date.year, month=want_date.month, day=want_date.day)
        if start < now:
            return _error(
                f"start {start.strftime('%Y-%m-%d %H:%M %Z')} is in the past (now is {now.strftime('%Y-%m-%d %H:%M %Z')})",
                "ask the user for a future time",
            )
        if duration_minutes is None or str(duration_minutes).strip() == "":
            duration = DEFAULT_DURATION
        else:
            duration = _int(duration_minutes, -1)
            if duration == -1:
                return _error(f"duration_minutes {duration_minutes!r} is not a number", "pass a whole number of minutes, e.g. 30")
        end = _add_minutes(start, duration)
        if end <= start:
            return _error(f"end would not be after start (duration {duration} minutes)", "use a positive duration, e.g. 30")
        if not (MIN_DURATION <= duration <= MAX_DURATION):
            return _error(f"duration {duration} minutes is outside {MIN_DURATION}..{MAX_DURATION}", "use a duration between 5 minutes and 8 hours")
        attendees, err = _coerce_attendees(attendee_emails, data, _typed_addresses(session))
        if err:
            refused = getattr(session, "refused_attendees", None)
            if isinstance(refused, list):
                if err in refused:
                    return _error(err + "; this was already refused once in this chat",
                                  "do not retry and do not offer to save it; tell the user once, plainly, that you need them to type the exact address")
                refused.append(err)
            return _error(err, "ask the user to type the exact email address in the chat, then draft again")
        inferred = False
        if not attendees:
            # The 7B model sometimes omits attendee_emails; a known contact named in
            # the title is the obvious attendee. The draft shows it before any write.
            attendees = _contacts_named_in(summary, data)
            inferred = bool(attendees)
        draft_id = f"d{secrets.token_hex(2)}"
        while draft_id in session.drafts:
            draft_id = f"d{secrets.token_hex(2)}"
        draft = {
            "draft_id": draft_id,
            "summary": summary,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "tz": str(tz),
            "attendees": attendees,
            "description": _str(description),
            "status": "DRAFT",
        }
        prev = session.drafts.get(session.pending_draft_id or "")
        out = dict(draft)  # the model's result: without the autonomy tags, so its input is unchanged
        autonomy.tag_draft(draft, prev)  # before prev is superseded: a revision counts as an edit
        if prev is not None and prev.get("status") == "DRAFT":
            prev["status"] = "SUPERSEDED"  # only the newest draft can be confirmed
        session.drafts[draft_id] = draft
        session.pending_draft_id = draft_id
        session.confirmed_draft_id = None  # a new draft always needs its own confirmation
        out["duration_minutes"] = duration
        out["when"] = _when(start, end)
        if date_note:
            out["date_note"] = date_note
        if inferred:
            out["attendees_note"] = "attendee_emails was empty; attendee taken from the known contact named in the title"
        out["note"] = "DRAFT only, nothing was created. Show this to the user and ask them to confirm before calling create_calendar_event."
        return _json(out)
    except Exception as e:
        return _error(f"could not draft the event ({type(e).__name__}: {e})", "try again with a title and an ISO start time")


@logged
def create_calendar_event(draft_id="") -> str:
    """Create a confirmed draft on the real calendar and return only what Google stored.
    Code-gated: refuses unless the session marked exactly this draft confirmed.

    Only the model's tool in CONFIRM_MODE=model (comparison runs). The Confirm click in the
    default CONFIRM_MODE=code goes through manage.commit_pending, which calls the same
    _insert_confirmed_event under the same gate."""
    try:
        session = obs.current_session.get()
        if session is None:
            return _error("no active session", "events can only be created inside a conversation turn")
        did = _str(draft_id)
        if not did:
            return _error("draft_id is missing", "there is no pending draft; call draft_calendar_event first and ask the user to confirm it")
        draft = session.drafts.get(did)
        if draft is None:
            return _error(f"no draft with id {did!r} exists in this conversation", "there is no pending draft; ask the user what to schedule and draft it first")
        if draft.get("event_id"):
            # Idempotent: never create twice for the same draft.
            return _already_created(draft)
        if draft.get("status") == "CANCELLED":
            return _error(f"draft {did} was cancelled by the user", "ask what to schedule and draft it again")
        if draft.get("status") != "DRAFT":
            return _error(f"draft {did} was replaced by a newer draft", "ask the user to confirm the latest draft")
        if getattr(session, "confirmed_draft_id", None) != did:
            return f"NOT CREATED: waiting for the user to confirm draft {did}"
        # One confirmation authorises at most one insert attempt.
        session.confirmed_draft_id = None
        # This call's log row is the decision row: who confirmed, was it edited, did Google confirm it.
        obs.annotate(**autonomy.commit_fields(draft), verified=False)
        try:
            return _insert_confirmed_event(session, draft)
        finally:
            ok, verified = event_create_outcome(draft)
            obs.annotate(verified=verified)
            autonomy.on_commit_result(draft, ok, verified)
    except Exception as e:
        return _error(f"could not create the event ({type(e).__name__}: {e})", "check Google Calendar and try confirming again")


def _already_created(draft: dict) -> str:
    """The stored event of a draft that was already created; nothing new is created."""
    stored = dict(draft.get("created") or {})
    stored["note"] = "This draft was already created earlier; returning the stored event, nothing new was created."
    return _json(stored)


def event_create_outcome(draft: dict) -> tuple[bool, bool]:
    """(written, verified) for an event_create draft after an insert attempt: written once the
    event id is recorded, verified only when Google's read-back confirmed it."""
    created = draft.get("created") or {}
    written = bool(draft.get("event_id"))
    return written, written and created.get("status") == "CREATED"


def _insert_confirmed_event(session, draft: dict) -> str:
    """Insert a confirmed event_create draft, read it back from Google and return the JSON
    (or ERROR string) the user is told about. The CALLER owns the gate (confirmed, single-use,
    not already created) and the decision row: this is deliberately not @logged and never
    annotates, so each write has exactly one decision row (commit_pending's or
    create_calendar_event's). May raise; the caller turns that into an ERROR string."""
    did = draft.get("draft_id")
    draft["insert_attempts"] = int(draft.get("insert_attempts") or 0) + 1
    cal = google_auth.get_services()["calendar"]
    # The event id is chosen here and kept on the draft, so a library retry (or a
    # later re-confirm after a lost response) can only hit 409 "already exists".
    planned = draft.get("planned_event_id") or _new_event_id()
    draft["planned_event_id"] = planned
    body = {
        "id": planned,
        "summary": draft["summary"],
        "start": {"dateTime": draft["start"], "timeZone": draft["tz"]},
        "end": {"dateTime": draft["end"], "timeZone": draft["tz"]},
    }
    if draft.get("attendees"):
        body["attendees"] = [{"email": a} for a in draft["attendees"]]
    if draft.get("description"):
        body["description"] = draft["description"]
    duplicate = False
    try:
        inserted = cal.events().insert(calendarId="primary", body=body, sendUpdates="none").execute(num_retries=2)
    except Exception as e:
        if _http_status(e) != 409:
            raise
        duplicate = True  # an earlier attempt was committed; verify it below
        inserted = {"id": planned}
    event_id = inserted.get("id") or planned
    if duplicate:
        try:
            got = cal.events().get(calendarId="primary", eventId=event_id).execute(num_retries=2)
        except Exception as e:
            return _error(f"event id {event_id} already exists but reading it back failed ({type(e).__name__}: {e})", "open Google Calendar to check, then confirm again")
        if got.get("status") == "cancelled" or got.get("summary") != draft["summary"]:
            draft["planned_event_id"] = _new_event_id()  # id collided with something else; use a fresh one next time
            return _error("the planned event id collided with another event", "confirm the draft again")
    else:
        got = None
    # Record the id immediately so a failure below can never cause a second insert.
    draft["event_id"] = event_id
    draft["status"] = "CREATED"
    if session.pending_draft_id == did:
        session.pending_draft_id = None
    if got is None:
        try:
            got = cal.events().get(calendarId="primary", eventId=event_id).execute(num_retries=2)
        except Exception as e:
            # Inserted for sure (we hold the id); the read-back is the only thing that failed.
            partial = {
                "status": "CREATED_UNVERIFIED",
                "id": event_id,
                "summary": inserted.get("summary") or draft["summary"],
                "start": inserted.get("start") or {"dateTime": draft["start"], "timeZone": draft["tz"]},
                "end": inserted.get("end") or {"dateTime": draft["end"], "timeZone": draft["tz"]},
                "attendees": [{"email": a} for a in draft.get("attendees") or []],
                "htmlLink": inserted.get("htmlLink"),
                "note": f"Created, but reading it back from Google Calendar failed ({type(e).__name__}: {e}); open the link to verify.",
            }
            draft["created"] = partial
            return _json(partial)
    tz = _tz()
    start_txt, _ = _fmt_google_time(got.get("start"), tz)
    end_txt, _ = _fmt_google_time(got.get("end"), tz)
    result = {
        "status": "CREATED",
        "id": got.get("id"),
        "summary": got.get("summary"),
        "start": got.get("start"),
        "end": got.get("end"),
        "when": f"{start_txt} to {end_txt} ({(got.get('start') or {}).get('timeZone') or tz})",
        "attendees": [
            {"email": a.get("email"), "responseStatus": a.get("responseStatus")} for a in got.get("attendees", []) or []
        ],
        "htmlLink": got.get("htmlLink"),
        "note": "Verified by reading the event back from Google Calendar."
        + (" (An earlier attempt had already stored it; nothing new was created.)" if duplicate else ""),
    }
    draft["created"] = result
    return _json(result)


CONTACT_FACT_RE = re.compile(
    r"(?i)^\s*(?:remember\s+(?:that\s+)?)?(?P<name>[a-z][\w .'\-]{0,60}?)(?:'s|\u2019s)?\s+"
    r"(?:e-?mail(?:\s+address)?|address)\s+(?:is|=|:)\s+(?P<addr>[^\s@]+@[^\s@]+?)\s*\.?\s*$"
)


def _save_contact(name, email_addr) -> str:
    session = obs.current_session.get()
    who = " ".join(_str(name).split())
    addr = _str(email_addr).strip().strip("<>\"'").removeprefix("mailto:").strip().lower()
    if not who:
        return _error("the contact name is missing", "ask the user for the person's name")
    if not EMAIL_RE.match(addr):
        return _error(f"{addr!r} is not a valid email address", "ask the user to type the exact address")
    data = memory.load()
    if addr not in _typed_addresses(session) and addr not in _saved_addresses(data):
        return _error(f"{addr} was not typed by the user in this chat, so it cannot be saved",
                      "ask the user to type the exact address they want saved")
    data, action = memory.add_contact(who, addr)
    return _json({
        "status": action.upper(),
        "contact": {"name": who, "email": addr},
        "note": f"Contact {action}. It can be used as a meeting attendee right away, and find_contact returns it by name.",
    })


@logged
def save_contact(name="", email="") -> str:
    """Save or update a contact (name + email) in data/memory.json. Only addresses the user typed."""
    try:
        return _save_contact(name, email)
    except Exception as e:
        return _error(f"could not save the contact ({type(e).__name__}: {e})", "try again")


@logged
def remember(fact="") -> str:
    """Persist a durable fact about the user to data/memory.json."""
    try:
        text = " ".join(_str(fact).split())
        if not text:
            return _error("fact is empty", "say what to remember")
        m = CONTACT_FACT_RE.match(text)
        if m:  # "Sam Lee's email is x@y.com" is a contact, not a free-text fact
            return _save_contact(m.group("name"), m.group("addr"))
        data = memory.append_fact(text)
        return _json(
            {
                "saved": text,
                "facts_count": len(data.get("facts", [])),
                "note": "Saved. It is used from the next new chat onwards.",
            }
        )
    except Exception as e:
        return _error(f"could not save the fact ({type(e).__name__}: {e})", "try again")


# --------------------------------------------------------------------------- registry

REGISTRY: dict = {
    "list_recent_emails": list_recent_emails,
    "read_email": read_email,
    "list_calendar_events": list_calendar_events,
    "find_contact": find_contact,
    "draft_calendar_event": draft_calendar_event,
    "create_calendar_event": create_calendar_event,
    "remember": remember,
    "save_contact": save_contact,
}

# JSON schemas proven in spike/ollama_probe.py (descriptions lightly tightened).
TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {"name": "list_recent_emails", "description": "Newest inbox messages as compact rows {id, from, subject, date, unread, group, snippet} plus the true unread_count. group is needs_reply (a real person), updates (notifications) or promotions (marketing). Use this before summarising the inbox, or with a query such as from:Ravi to find a specific email.", "parameters": {"type": "object", "properties": {"max_results": {"type": "integer", "default": 10}, "query": {"type": "string", "default": "in:inbox newer_than:3d"}}}}},
    {"type": "function", "function": {"name": "read_email", "description": "Decoded body of one message when the snippet is not enough.", "parameters": {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]}}},
    {"type": "function", "function": {"name": "list_calendar_events", "description": "Primary-calendar events (with ids) in the next N days, or for one whole day with on_date.", "parameters": {"type": "object", "properties": {"days_ahead": {"type": "integer", "default": 7}, "on_date": {"type": "string", "description": "YYYY-MM-DD, today, tomorrow or a weekday"}}}}},
    {"type": "function", "function": {"name": "find_contact", "description": "Candidate email addresses for a first name. If 0 or 2+ candidates come back, ask the user which one before drafting.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "draft_calendar_event", "description": "Validate and store a proposed event under a draft_id and return it for the user to confirm. Never writes to the calendar. Omit duration_minutes unless the user gave one (default 30).", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "start_iso": {"type": "string", "description": "ISO 8601 local start, e.g. 2026-10-14T15:00"}, "duration_minutes": {"type": "integer", "default": 30}, "attendee_emails": {"type": "array", "items": {"type": "string"}}, "description": {"type": "string"}}, "required": ["title", "start_iso"]}}},
    {"type": "function", "function": {"name": "create_calendar_event", "description": "Create a draft on the real calendar. Only call this after the user has explicitly confirmed the draft. Takes only a draft_id returned earlier by draft_calendar_event in this conversation. Returns what Google stored.", "parameters": {"type": "object", "properties": {"draft_id": {"type": "string"}}, "required": ["draft_id"]}}},
    {"type": "function", "function": {"name": "remember", "description": "Persist a durable fact about the user, such as a preference or an alias like 'Andy means Andy Chen'. For a person's email address use save_contact instead.", "parameters": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}}},
    {"type": "function", "function": {"name": "save_contact", "description": "Save a person as a contact so they can be invited to meetings and found by name. Use it when the user asks to add or save someone. The email must be an address the user typed in this chat.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Full name, e.g. Sam Lee"}, "email": {"type": "string", "description": "Email address exactly as the user typed it"}}, "required": ["name", "email"]}}},
]

# Write actions (email send/modify, event change, free time) live in manage.py.
from assistant import manage  # noqa: E402  (imported last: manage uses the helpers above)

REGISTRY.update(manage.REGISTRY)
TOOL_SCHEMAS.extend(manage.TOOL_SCHEMAS)
# The model never gets a tool that writes. The Confirm click runs commit_pending for every card,
# new events included; CONFIRM_MODE=model (comparison runs only) puts create_calendar_event back.
if os.environ.get("CONFIRM_MODE", "code").strip().lower() != "model":
    TOOL_SCHEMAS[:] = [t for t in TOOL_SCHEMAS if t["function"]["name"] != "create_calendar_event"]
