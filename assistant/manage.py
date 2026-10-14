"""Write actions for Gmail and Google Calendar, every one behind the Confirm card.

The model can only PREPARE an action: each tool here validates the request, works
out exactly what would change, and stores it as a pending card on the session.
commit_pending() is the only function that writes to Google, for every card including
a new calendar event (tools.draft_calendar_event's drafts, kind event_create), the
model is never given it, and it refuses unless the server marked exactly that card
confirmed (the user's click). tools.create_calendar_event remains only as the model's
tool for CONFIRM_MODE=model comparison runs, behind the same gate. Nothing here can permanently delete mail: the OAuth scope is
gmail.modify, which Google does not allow to bypass Trash, and "delete" means Trash.

Recipients follow the attendee rule from tools.py: a saved contact, an address the
user typed in this chat, (for a reply) the sender of the email being answered, or
(for a follow-up nudge) a recipient of the user's own sent message being followed up.
An address that only appears inside some email can never become a recipient.

Every card is tagged for autonomy.py (workflow, origin, proposed_hash, edited), and the
commit row in logs/tools.jsonl carries the decision fields the promotion streak reads.
"""

from __future__ import annotations

import base64
import email.utils
import html
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import assistant.tools as base
from assistant import autonomy, context, google_auth, memory, obs
from assistant.obs import logged

_error = base._error
_json = base._json
_str = base._str

MAX_TARGETS = 50
CARD_LIST_MAX = 8
WORK_START = "09:00"
WORK_END = "18:00"

# --------------------------------------------------------------------------- pending cards


def _session():
    return obs.current_session.get()


def _card(title: str, lines: list[str], confirm_label: str, note: str, *, body: str | None = None,
          alt: dict | None = None, danger: bool = False) -> dict:
    """What the web page shows: a title, detail lines, an optional body, and the buttons."""
    return {"title": title, "lines": lines, "body": body, "confirm_label": confirm_label,
            "alt": alt, "danger": danger, "note": note}


def _new_pending(session, kind: str, card: dict, *, origin: str | None = None, **payload) -> dict:
    """Store a pending action; only the newest one can be confirmed. origin defaults to
    obs.current_origin ("asked" unless a brief or follow-up code path set it)."""
    did = f"d{secrets.token_hex(2)}"
    while did in session.drafts:
        did = f"d{secrets.token_hex(2)}"
    prev = session.drafts.get(session.pending_draft_id or "")
    draft = {"draft_id": did, "kind": kind, "status": "DRAFT", "card": card, **payload}
    if origin:
        draft["origin"] = origin
    autonomy.tag_draft(draft, prev)  # before prev is superseded: a revision counts as an edit
    if prev is not None and prev.get("status") == "DRAFT":
        prev["status"] = "SUPERSEDED"
    session.drafts[did] = draft
    session.pending_draft_id = did
    session.confirmed_draft_id = None  # a new card always needs its own click
    return draft


def _prepared(draft: dict) -> str:
    """Tool result for the model: what is on the card, and that nothing happened yet."""
    card = draft["card"]
    return _json({
        "draft_id": draft["draft_id"],
        "status": "READY_TO_CONFIRM",
        "card": {"title": card["title"], "lines": card["lines"], "body": card.get("body")},
        "note": "Nothing has been done yet. Tell the user it is ready and that it happens only when they click "
                f"'{card['confirm_label']}' on the card. Do not say it was done.",
    })


def _lines_capped(items: list[str]) -> list[str]:
    shown = items[:CARD_LIST_MAX]
    if len(items) > CARD_LIST_MAX:
        shown.append(f"... and {len(items) - CARD_LIST_MAX} more")
    return shown


# --------------------------------------------------------------------------- dates


def _parse_day(text: str, today: date) -> date | None:
    """YYYY-MM-DD, today, tomorrow, or a weekday name (the next one, today counts)."""
    t = (_str(text) or "").strip().lower()
    if not t:
        return None
    if t in {"today", "tonight"}:
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t in base.WEEKDAYS:
        want = base.WEEKDAYS.index(t)
        return today + timedelta(days=(want - today.weekday()) % 7)
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        return None


def _hhmm(text: str, default: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$", _str(text) or default)
    if not m:
        m = re.match(r"^(\d{1,2}):(\d{2})$", default)
    return int(m.group(1)), int(m.group(2) or 0)


def _event_when(ev: dict, tz) -> str:
    start, all_day = base._fmt_google_time(ev.get("start"), tz)
    end, _ = base._fmt_google_time(ev.get("end"), tz)
    if all_day:
        return f"{start} (all day)"
    return f"{start} to {end[11:] if end[:10] == start[:10] else end}"


# --------------------------------------------------------------------------- email: draft


PLACEHOLDER_RE = re.compile(r"\[(?:your|recipient|insert|name|date|time|company|sender)[^\]]{0,30}\]", re.I)


def _message_full(gmail, mid: str) -> tuple[dict, dict, str]:
    msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute(num_retries=2)
    payload = msg.get("payload") or {}
    hdr = base._headers(payload)
    plain: list[str] = []
    htmls: list[str] = []
    others: list[str] = []
    base._walk_parts(payload, plain, htmls, others)
    if plain:
        body = "\n".join(plain)
    elif htmls:
        body = base._strip_html("\n".join(htmls))
    else:
        body = html.unescape(msg.get("snippet") or "")
    return msg, hdr, body.strip()


@logged
def draft_email(to=None, subject="", body="", cc=None, reply_to_message_id="", forward_message_id="") -> str:
    """Prepare an email (new, reply or forward) as a card. Nothing is sent until the user clicks Send."""
    try:
        session = _session()
        if session is None:
            return _error("no active session", "emails can only be drafted inside a conversation turn")
        data = memory.load()
        typed = base._typed_addresses(session)
        reply_mid, fwd_mid = _str(reply_to_message_id), _str(forward_message_id)
        if reply_mid and fwd_mid:
            return _error("both reply_to_message_id and forward_message_id were given", "pass only one of them")
        text = _str(body).strip()
        name = str(data.get("name") or "").strip()
        if name:
            text = re.sub(r"\[\s*your\s+name\s*\]", name, text, flags=re.I)
        mode, thread_id, in_reply_to, references = "new", None, None, None
        allowed_extra: set[str] = set()
        if reply_mid or fwd_mid:
            gmail = google_auth.get_services()["gmail"]
            msg, hdr, orig_body = _message_full(gmail, reply_mid or fwd_mid)
            orig_subject = " ".join(hdr.get("subject", "").split())
            if reply_mid:
                mode = "reply"
                thread_id = msg.get("threadId")
                in_reply_to = hdr.get("message-id") or None
                references = " ".join(x for x in [hdr.get("references", ""), in_reply_to or ""] if x).strip() or None
                reply_to = hdr.get("reply-to") or hdr.get("from", "")
                reply_addrs = sorted({a.lower() for _, a in email.utils.getaddresses([reply_to]) if a})
                sender_addrs = {a.lower() for _, a in email.utils.getaddresses([hdr.get("from", ""), reply_to]) if a}
                allowed_extra = set(reply_addrs)
                given = {email.utils.parseaddr(str(x))[1].lower() for x in _as_list_keep(to)} - {""}
                # Like Gmail's Reply button: answering the sender means the Reply-To address
                # (Basecamp, Jira and similar relays need it to land in the thread).
                if not to or given <= sender_addrs:
                    to = reply_addrs
                subj = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
            else:
                mode = "forward"
                subj = orig_subject if orig_subject.lower().startswith(("fwd:", "fw:")) else f"Fwd: {orig_subject}"
                block = (
                    "\n\n---------- Forwarded message ----------\n"
                    f"From: {hdr.get('from', '')}\nDate: {hdr.get('date', '')}\nSubject: {orig_subject}\nTo: {hdr.get('to', '')}\n\n"
                    f"{orig_body[: base.BODY_MAX_CHARS]}"
                )
                text = (text + block).strip()
        else:
            subj = " ".join(_str(subject).split())
            if not subj:
                # The 7B model often leaves the subject out; take the body's first line instead of
                # failing the turn. The card shows the subject before anything is sent.
                first = next((ln.strip() for ln in (text or "").splitlines()
                              if ln.strip() and not re.match(r"(?i)^(hi|hello|hey|dear)\b", ln.strip())), "")
                subj = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0].rstrip(".!?")[:70].strip()
            if not subj:
                return _error("subject is missing", "write a short subject line and draft again")
        if not text:
            return _error("body is missing", "write the email text the user asked for and draft again")
        to_list, err = base._coerce_attendees(to, data, typed | allowed_extra)
        if err:
            return _error(err.replace("an attendee", "a recipient"), "ask the user to type the exact email address in the chat, then draft again")
        if not to_list:
            return _error("there is no recipient", "ask the user who the email should go to")
        cc_list: list[str] = []
        if cc:
            cc_list, err = base._coerce_attendees(cc, data, typed | allowed_extra)
            if err:
                return _error(err.replace("an attendee", "a recipient"), "ask the user to type the exact email address in the chat, then draft again")
        placeholders = PLACEHOLDER_RE.findall(text)
        lines = [f"To: {', '.join(to_list)}"] + ([f"Cc: {', '.join(cc_list)}"] if cc_list else []) + [f"Subject: {subj}"]
        note = "Nothing has been sent yet."
        if placeholders:
            note += f" Check the text first: it still contains {placeholders[0]}."
        if mode == "forward":
            note += " Attachments are not forwarded."
        title = {"new": "Email draft", "reply": "Reply draft", "forward": "Forward draft"}[mode]
        card = _card(title, lines, "Send", note, body=text, alt={"label": "Save to Gmail drafts", "variant": "save_draft"})
        draft = _new_pending(session, "email_send", card, to=to_list, cc=cc_list, subject=subj, body=text,
                             thread_id=thread_id, in_reply_to=in_reply_to, references=references, mode=mode,
                             source_message_id=reply_mid or fwd_mid or None)
        # A display name for voice read-backs ("Here's the reply to Ravi"); never used to send.
        draft["to_name"] = _display_name(hdr.get("from", "") if mode == "reply" else "", to, to_list, data)
        return _prepared(draft)
    except Exception as e:
        return _error(f"could not draft the email ({type(e).__name__}: {e})", "try again")


def _display_name(from_header: str, given, to_list: list[str], data: dict) -> str:
    """A person's name for voice read-backs: the replied-to sender's display name, a name the
    request gave ("Sam Lee <sam.lee@example.org>"), a saved contact, or ""."""
    disp = email.utils.parseaddr(from_header or "")[0]
    if not disp:
        for raw in _as_list_keep(given):
            name, addr = email.utils.parseaddr(raw)
            if name:
                disp = name
                break
            if raw and "@" not in raw:
                disp = raw
                break
    if not disp and to_list:
        want = to_list[0].lower()
        disp = next((str(c.get("name") or "") for c in data.get("contacts", []) or []
                     if isinstance(c, dict) and str(c.get("email") or "").lower() == want), "")
    disp = re.sub(r"\s*\([^)]*\)", "", disp or "")  # "Ravi Mehta (Basecamp)" -> "Ravi Mehta"
    return " ".join(disp.replace('"', "").split())[:60]


def draft_send_saved(prev: dict, session=None) -> dict | None:
    """After a verified voice "confirm" saved an email to Gmail drafts: a new pending email_send
    card that sends THAT saved draft (Gmail drafts.send). Same recipients, subject, body and
    thread as the card it came from. Called by code only (voice_policy.py), never a model tool;
    it is an ordinary card behind the same gate. None when there is no verified saved draft."""
    session = session or _session()
    done = (prev or {}).get("committed") or {}
    gid = done.get("gmail_draft_id")
    if session is None or not gid or done.get("status") != "SAVED_DRAFT" or not done.get("verified"):
        return None
    who = prev.get("to_name") or ", ".join(prev.get("to") or [])
    lines = [f"To: {', '.join(prev.get('to') or [])}"] + ([f"Cc: {', '.join(prev['cc'])}"] if prev.get("cc") else []) \
        + [f"Subject: {prev.get('subject')}"]
    card = _card("Send the saved draft", lines, "Send", f"It is saved in your Gmail drafts. Nothing has been sent yet; Send sends it to {who}.",
                 body=prev.get("body"), alt=None)
    return _new_pending(session, "email_send", card, origin=prev.get("origin"), to=list(prev.get("to") or []),
                        cc=list(prev.get("cc") or []), subject=prev.get("subject"), body=prev.get("body"),
                        thread_id=prev.get("thread_id"), in_reply_to=prev.get("in_reply_to"),
                        references=prev.get("references"), mode=prev.get("mode"),
                        source_message_id=prev.get("source_message_id"), gmail_draft_id=gid,
                        to_name=prev.get("to_name") or "", saved_from=prev.get("draft_id"))


@logged
def draft_nudge(message_id: str = "", body: str = "") -> str:
    """A follow-up on the user's OWN sent message: a reply in its thread to that message's
    To and Cc (never the user), tagged workflow follow_ups / origin followup. Made by code
    from the brief's Nudge button, never a model tool. Nothing is sent until Send."""
    try:
        session = _session()
        if session is None:
            return _error("no active session", "a follow-up can only be drafted inside a conversation turn")
        mid = _str(message_id)
        if not mid:
            return _error("message_id is missing", "pass the id of the user's own sent message to follow up")
        data = memory.load()
        text = _str(body).strip()
        name = str(data.get("name") or "").strip()
        if name:
            text = re.sub(r"\[\s*your\s+name\s*\]", name, text, flags=re.I)
        if not text:
            return _error("body is missing", "write the follow-up text and draft again")
        gmail = google_auth.get_services()["gmail"]
        msg, hdr, _orig_body = _message_full(gmail, mid)
        own = base._own_address()
        from_addr = email.utils.parseaddr(hdr.get("from", ""))[1].lower()
        # Only the user's own message: its recipients are people the user already chose to write to.
        # Gmail's SENT label, not the From header, which an incoming email can forge.
        if "SENT" not in (msg.get("labelIds") or []):
            return _error(f"message {mid} was not sent by the user", "a nudge can only follow up the user's own sent email")
        mine = {a for a in (own, from_addr) if a}  # From of a sent message is the user (or a send-as alias)
        to_list = list(dict.fromkeys(a.lower() for _, a in email.utils.getaddresses([hdr.get("to", "")]) if a and a.lower() not in mine))
        cc_list = [a for a in dict.fromkeys(a.lower() for _, a in email.utils.getaddresses([hdr.get("cc", "")]) if a and a.lower() not in mine)
                   if a not in to_list]
        if not to_list and cc_list:
            to_list, cc_list = cc_list, []
        if not to_list:
            return _error(f"message {mid} has no recipient other than the user", "there is nobody to follow up with")
        # Recipient rule, follow-up case: exactly the addresses on the user's own message.
        followed_up = set(to_list) | set(cc_list)
        typed = base._typed_addresses(session)
        to_list, err = base._coerce_attendees(to_list, data, typed | followed_up)
        if not err and cc_list:
            cc_list, err = base._coerce_attendees(cc_list, data, typed | followed_up)
        if err:
            return _error(err.replace("an attendee", "a recipient"), "ask the user to type the exact email address in the chat, then draft again")
        orig_subject = " ".join(hdr.get("subject", "").split())
        subj = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
        in_reply_to = hdr.get("message-id") or None
        references = " ".join(x for x in [hdr.get("references", ""), in_reply_to or ""] if x).strip() or None
        lines = [f"To: {', '.join(to_list)}"] + ([f"Cc: {', '.join(cc_list)}"] if cc_list else []) + [f"Subject: {subj}"]
        note = "Nothing has been sent yet. It goes in the same thread as your earlier email."
        placeholders = PLACEHOLDER_RE.findall(text)
        if placeholders:
            note += f" Check the text first: it still contains {placeholders[0]}."
        card = _card("Follow-up draft", lines, "Send", note, body=text, alt={"label": "Save to Gmail drafts", "variant": "save_draft"})
        draft = _new_pending(session, "email_send", card, origin="followup", to=to_list, cc=cc_list, subject=subj, body=text,
                             thread_id=msg.get("threadId"), in_reply_to=in_reply_to, references=references, mode="nudge",
                             source_message_id=mid)
        draft["to_name"] = _display_name(hdr.get("to", ""), None, to_list, data)
        return _prepared(draft)
    except Exception as e:
        return _error(f"could not draft the follow-up ({type(e).__name__}: {e})", "try again")


# --------------------------------------------------------------------------- email: modify

_ACTION_ALIASES = {
    "archive": "archive", "unarchive": "unarchive", "move_to_inbox": "unarchive", "inbox": "unarchive",
    "read": "mark_read", "mark_read": "mark_read", "mark_as_read": "mark_read",
    "unread": "mark_unread", "mark_unread": "mark_unread", "mark_as_unread": "mark_unread",
    "star": "star", "unstar": "unstar",
    "trash": "trash", "delete": "trash", "bin": "trash", "remove": "trash",
    "untrash": "untrash", "restore": "untrash",
    "label": "label", "add_label": "label", "unlabel": "unlabel", "remove_label": "unlabel",
}
_LABEL_OPS = {
    "archive": ([], ["INBOX"]), "unarchive": (["INBOX"], []),
    "mark_read": ([], ["UNREAD"]), "mark_unread": (["UNREAD"], []),
    "star": (["STARRED"], []), "unstar": ([], ["STARRED"]),
}
_VERBS = {
    "archive": "Archive", "unarchive": "Move to inbox", "mark_read": "Mark as read", "mark_unread": "Mark as unread",
    "star": "Star", "unstar": "Unstar", "trash": "Move to Trash", "untrash": "Restore from Trash",
    "label": "Add label", "unlabel": "Remove label",
}
_DONE = {
    "archive": "Archived", "unarchive": "Moved back to the inbox", "mark_read": "Marked as read",
    "mark_unread": "Marked as unread", "star": "Starred", "unstar": "Unstarred", "trash": "Moved to Trash",
    "untrash": "Restored from Trash", "label": "Labelled", "unlabel": "Removed the label from",
}


def _as_list_keep(value) -> list[str]:
    """Recipients as a list without splitting on spaces ("Name <addr>" stays whole)."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[,;\n]+", value) if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = text
        if isinstance(value, str):
            return [v.strip() for v in re.split(r"[,\s]+", value) if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


# The user's own words win over the model's arguments when they are explicit: the 7B model
# sometimes picks the right emails but the wrong verb ("unlabel" for "archive all promotions").
# The card still shows exactly what will happen, and nothing runs without the click.
_USER_ACTIONS = [
    ("mark_unread", re.compile(r"(?i)\bmark\b(?:\s+\S+){0,5}?\s+(?:as\s+)?unread\b")),
    ("mark_read", re.compile(r"(?i)\bmark\b(?:\s+\S+){0,5}?\s+(?:as\s+)?read\b")),
    ("untrash", re.compile(r"(?i)\b(?:restore|untrash|recover)\b")),
    ("trash", re.compile(r"(?i)\b(?:trash|delete|bin)\b")),
    ("unarchive", re.compile(r"(?i)\bunarchive\b|\bmove\b(?:\s+\S+){0,4}?\s+back\s+to\s+(?:the\s+|my\s+)?inbox\b")),
    ("archive", re.compile(r"(?i)\barchive\b")),
    ("unstar", re.compile(r"(?i)\bunstar\b")),
    ("star", re.compile(r"(?i)\bstar\b")),
]
_USER_GROUPS = [
    ("promotions", re.compile(r"(?i)\bpromo(?:tion(?:al|s)?|s)?\b|\bmarketing\b")),
    ("updates", re.compile(r"(?i)\b(?:updates|notifications)\b")),
    ("needs_reply", re.compile(r"(?i)\bneeds?\s+(?:a\s+)?reply\b")),
]


_SYSTEM_NOTES = ("User clicked", "You did not call", "You wrote the email in the chat")


def _latest_typed_text(session) -> str:
    """The user's latest real message: button clicks and nudges recorded as user turns are skipped."""
    for m in reversed(getattr(session, "messages", []) or []):
        if m.get("role") == "user":
            text = base._user_text(str(m.get("content", "")))
            if not text.startswith(_SYSTEM_NOTES):
                return text
    return ""


def _user_said(session) -> tuple[str | None, str | None]:
    """(action, group) the user's latest typed message names explicitly, else None for each."""
    text = _latest_typed_text(session) if session is not None else ""
    act = next((a for a, rx in _USER_ACTIONS if rx.search(text)), None)
    grp = next((g for g, rx in _USER_GROUPS if rx.search(text)), None)
    return act, grp


def _resolve_targets(gmail, message_ids, query: str, group: str, action: str) -> tuple[list[str], str]:
    ids = _as_list(message_ids)
    if ids:
        return list(dict.fromkeys(ids))[:MAX_TARGETS], "ids"
    if query:
        include_trash = action == "untrash" or "in:trash" in query.lower()
        listing = gmail.users().messages().list(userId="me", q=context.gmail_scope(query), maxResults=MAX_TARGETS, includeSpamTrash=include_trash).execute(num_retries=2)
        return [m["id"] for m in listing.get("messages", []) or []], f"query {query!r}"
    g = (group or "").strip().lower().replace(" ", "_").replace("-", "_")
    g = {"promotion": "promotions", "update": "updates", "people": "needs_reply", "needs_a_reply": "needs_reply"}.get(g, g)
    if g in base.INBOX_GROUPS:
        listing = gmail.users().messages().list(userId="me", q=context.gmail_scope("in:inbox newer_than:7d"), maxResults=MAX_TARGETS).execute(num_retries=2)
        found = [m["id"] for m in listing.get("messages", []) or []]
        meta = base._batch_get(gmail, found, base.LIST_HEADERS)
        own_first = base._own_first_name()
        picked = [mid for mid in found if mid in meta and base._mail_group(
            base._headers(meta[mid].get("payload") or {}).get("from", ""), meta[mid].get("labelIds") or [],
            base._headers(meta[mid].get("payload") or {}), own_first) == g]
        return picked, f"the {g.replace('_', ' ')} group in the last 7 days"
    return [], ""


def _label_id(gmail, name: str) -> str | None:
    for lab in gmail.users().labels().list(userId="me").execute(num_retries=2).get("labels", []) or []:
        if str(lab.get("name", "")).lower() == name.lower():
            return lab.get("id")
    return None


@logged
def modify_emails(action="", message_ids=None, query="", group="", label="") -> str:
    """Prepare a mailbox change (archive, read/unread, star, label, trash, restore) as a card."""
    try:
        session = _session()
        if session is None:
            return _error("no active session", "email changes can only be prepared inside a conversation turn")
        return _prepare_modify(session, action, message_ids, query, group, label, use_user_words=True)
    except Exception as e:
        return _error(f"could not prepare the email change ({type(e).__name__}: {e})", "try again")


def _prepare_modify(session, action, message_ids, query, group, label, *, use_user_words: bool) -> str:
    """The body of modify_emails. use_user_words=False for code paths (a brief button),
    where the user's last typed message is about something else and must not change the verb."""
    act = _ACTION_ALIASES.get(_str(action).strip().lower().replace(" ", "_"))
    label_name = " ".join(_str(label).split())
    said_act, said_group = _user_said(session) if use_user_words else (None, None)
    if said_act and act != said_act and not (said_act in ("star", "unstar") and act in ("label", "unlabel")):
        act = said_act  # the user's explicit verb wins
        if label_name.lower().replace(" ", "_") in base.INBOX_GROUPS + ("promotion", "update"):
            label_name = ""  # "promotions" was the group, not a label
    if said_group and not _as_list(message_ids) and not _str(query) and not _str(group):
        group = said_group
    if label_name.lower().replace(" ", "_") in base.INBOX_GROUPS and act in ("label", "unlabel") and said_group:
        return _error(f"'{label_name}' is a group of emails, not a label", "ask the user what to do with those emails: archive, mark read, or trash")
    if not act:
        return _error(f"unknown action {action!r}", f"use one of: {', '.join(sorted(set(_ACTION_ALIASES.values())))}")
    if act in ("label", "unlabel") and not label_name:
        return _error("label is missing", "pass the label name, e.g. Clients")
    gmail = google_auth.get_services()["gmail"]
    ids, source = _resolve_targets(gmail, message_ids, _str(query), _str(group), act)
    if not source:
        return _error("no emails were specified", "pass message_ids from list_recent_emails, a Gmail query such as from:cloudflare, or group=promotions")
    if not ids:
        return _error(f"no emails matched {source}", "tell the user nothing matched and ask what they meant")
    will_create_label = False
    if act in ("label", "unlabel"):
        lid = _label_id(gmail, label_name)
        if lid is None and act == "unlabel":
            return _error(f"there is no label called {label_name!r}", "ask the user for the exact label name")
        will_create_label = lid is None
    meta = base._batch_get(gmail, ids, ["From", "Subject"])
    items = []
    for mid in ids:
        hdr = base._headers((meta.get(mid) or {}).get("payload") or {})
        disp, addr = email.utils.parseaddr(hdr.get("from", ""))
        items.append(f"{' '.join((disp or addr or '?').split())} - {' '.join(hdr.get('subject', '(no subject)').split())[:80]}")
    verb = _VERBS[act] + (f" '{label_name}'" if act in ("label", "unlabel") else "")
    notes = {
        "trash": "Moves to Trash: Gmail keeps it for 30 days. Nothing is permanently deleted.",
        "archive": "Archived mail stays in All Mail and in search.",
    }
    note = "Nothing has changed yet. " + notes.get(act, "")
    if will_create_label:
        note += f" Creates the label '{label_name}'."
    card = _card(f"{verb}: {len(ids)} email(s)", _lines_capped(items), f"{_VERBS[act]} {len(ids)}", note.strip(),
                 danger=act == "trash")
    draft = _new_pending(session, "email_modify", card, action=act, ids=ids, label_name=label_name, source=source)
    return _prepared(draft)


@logged
def draft_triage(action: str = "", group: str = "", query: str = "", origin: str = "brief", message_ids=None) -> str:
    """A mailbox-change card made by code (the brief's "Archive all" button), tagged with
    its origin for the autonomy policy. Same card and result as modify_emails; never a model tool.
    message_ids pins the card to exactly the emails the brief showed, so its count matches the brief."""
    try:
        session = _session()
        if session is None:
            return _error("no active session", "email changes can only be prepared inside a conversation turn")
        ids = [str(i) for i in (message_ids or []) if str(i).strip()]
        if not ids and not _str(group) and not _str(query):
            return _error("no emails were specified", "pass group=promotions, a Gmail query or message ids")
        token = obs.current_origin.set(_str(origin) or "brief")
        try:
            if ids:
                return _prepare_modify(session, action, ids, "", "", "", use_user_words=False)
            return _prepare_modify(session, action, None, _str(query), _str(group), "", use_user_words=False)
        finally:
            obs.current_origin.reset(token)
    except Exception as e:
        return _error(f"could not prepare the email change ({type(e).__name__}: {e})", "try again")


# --------------------------------------------------------------------------- calendar: change existing events

_EVENT_ACTIONS = {
    "update": "update", "move": "update", "reschedule": "update", "edit": "update", "rename": "update", "change": "update",
    "delete": "delete", "cancel": "delete", "remove": "delete",
    "rsvp": "rsvp", "accept": "rsvp", "decline": "rsvp", "tentative": "rsvp", "maybe": "rsvp",
}
_RSVP = {"accept": "accepted", "accepted": "accepted", "yes": "accepted", "decline": "declined", "declined": "declined",
         "no": "declined", "tentative": "tentative", "maybe": "tentative"}


def _find_event(cal, event_id: str, event_query: str, on_date: str, tz, now: datetime) -> tuple[dict | None, str | None]:
    if event_id:
        ev = cal.events().get(calendarId="primary", eventId=event_id).execute(num_retries=2)
        if ev.get("status") == "cancelled":
            return None, f"event {event_id} is already deleted"
        return ev, None
    day = _parse_day(on_date, now.date())
    if day is not None:
        t_min = datetime.combine(day, datetime.min.time(), tz)
        t_max = t_min + timedelta(days=1)
    else:
        t_min, t_max = now - timedelta(days=1), now + timedelta(days=30)
    query = " ".join(_str(event_query).split())
    resp = cal.events().list(calendarId="primary", timeMin=t_min.isoformat(), timeMax=t_max.isoformat(), singleEvents=True,
                             orderBy="startTime", maxResults=50).execute(num_retries=2)
    events = [e for e in resp.get("items", []) or [] if e.get("status") != "cancelled"]
    if query:
        toks = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in {"the", "my", "a", "an", "with", "call", "meeting", "event"}]
        if toks:
            events = [e for e in events if all(t in f"{e.get('summary', '')} {' '.join(a.get('email', '') for a in e.get('attendees', []) or [])}".lower() for t in toks)]
    if not events:
        where = f" on {day.isoformat()}" if day else " in the next 30 days"
        return None, f"no event matched {query or 'that'}{where}"
    if len(events) > 1:
        options = "; ".join(f"[{e.get('id')}] {e.get('summary', '(no title)')} ({_event_when(e, tz)})" for e in events[:6])
        return None, f"{len(events)} events match: {options}. Ask the user which one, then pass its event_id"
    return events[0], None


@logged
def draft_event_change(action="update", event_id="", event_query="", on_date="", new_start_iso="",
                       new_duration_minutes=None, new_title="", rsvp="") -> str:
    """Prepare a change to an existing event (move/rename, delete, or RSVP) as a card."""
    try:
        session = _session()
        if session is None:
            return _error("no active session", "events can only be changed inside a conversation turn")
        word = _str(action).strip().lower() or "update"
        act = _EVENT_ACTIONS.get(word)
        if not act:
            return _error(f"unknown action {action!r}", "use update, delete or rsvp")
        tz = base._tz()
        now = base._now()
        cal = google_auth.get_services()["calendar"]
        ev, err = _find_event(cal, _str(event_id), _str(event_query), _str(on_date), tz, now)
        if err:
            return _error(err, "ask the user which event they mean")
        summary = ev.get("summary") or "(no title)"
        when = _event_when(ev, tz)
        attendees = [a.get("email") for a in ev.get("attendees", []) or [] if a.get("email")]
        lines = [f"Event: {summary}", f"When: {when}"] + ([f"Guests: {', '.join(attendees)}"] if attendees else [])
        if act == "delete":
            card = _card("Delete event", lines, "Delete event",
                         "Nothing has changed yet. It moves to Google Calendar's trash; guests are not emailed.", danger=True)
            draft = _new_pending(session, "event_delete", card, target_event_id=ev["id"], summary=summary, attendees=attendees,
                                 when_iso=(ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date"))
            return _prepared(draft)
        if act == "rsvp":
            response = _RSVP.get((_str(rsvp) or word).strip().lower())
            if not response:
                return _error("the RSVP answer is missing", "pass rsvp=accept, decline or tentative")
            own = base._own_address()
            if (ev.get("organizer") or {}).get("self") or str((ev.get("organizer") or {}).get("email", "")).lower() == own:
                return _error("the user organises this event, so there is nothing to answer", "offer to change or delete it instead")
            if not any(a.get("self") or str(a.get("email", "")).lower() == own for a in ev.get("attendees", []) or []):
                return _error("the user is not on this event's guest list", "ask which event they meant")
            card = _card(f"Answer invite: {response}", lines, response.capitalize(),
                         "Nothing has changed yet. The organiser sees your answer on their calendar; no email is sent.")
            draft = _new_pending(session, "event_rsvp", card, target_event_id=ev["id"], response=response, summary=summary,
                                 when_iso=(ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date"))
            return _prepared(draft)
        # update
        if "dateTime" not in (ev.get("start") or {}):
            if _str(new_start_iso) or new_duration_minutes not in (None, ""):
                return _error("this is an all-day event; only its title can be changed here", "change the title, or move it in Google Calendar")
        old_start = datetime.fromisoformat((ev.get("start") or {}).get("dateTime")).astimezone(tz) if "dateTime" in (ev.get("start") or {}) else None
        old_end = datetime.fromisoformat((ev.get("end") or {}).get("dateTime")).astimezone(tz) if "dateTime" in (ev.get("end") or {}) else None
        patch: dict = {}
        change_lines = [f"Event: {summary}"]
        new_title_s = " ".join(_str(new_title).split())
        if new_title_s and new_title_s != summary:
            patch["summary"] = new_title_s
            change_lines.append(f"Title: {summary} → {new_title_s}")
        if old_start is not None:
            start = old_start
            if _str(new_start_iso):
                parsed = base._parse_start(_str(new_start_iso), tz)
                if parsed is None:
                    return _error(f"could not parse new_start_iso {new_start_iso!r}", "pass ISO 8601 local time, e.g. 2026-10-14T17:00")
                start = parsed
            minutes = int((old_end - old_start).total_seconds() // 60) if old_end else base.DEFAULT_DURATION
            if new_duration_minutes not in (None, ""):
                minutes = base._int(new_duration_minutes, minutes)
            if not (base.MIN_DURATION <= minutes <= base.MAX_DURATION):
                return _error(f"duration {minutes} minutes is outside {base.MIN_DURATION}..{base.MAX_DURATION}", "use a duration between 5 minutes and 8 hours")
            end = base._add_minutes(start, minutes)
            if start != old_start or end != old_end:
                if start < now.replace(second=0, microsecond=0):
                    return _error(f"the new start {start:%Y-%m-%d %H:%M} is in the past", "ask the user for a future time")
                patch["start"] = {"dateTime": start.isoformat(), "timeZone": str(tz)}
                patch["end"] = {"dateTime": end.isoformat(), "timeZone": str(tz)}
                change_lines.append(f"When: {when} → {base._when(start, end)}")
        if not patch:
            return _error("nothing would change", "ask the user what to change: the time, the length or the title")
        if attendees:
            change_lines.append(f"Guests: {', '.join(attendees)}")
        card = _card("Change event", change_lines, "Save change",
                     "Nothing has changed yet." + (" Guests are not emailed about the change." if attendees else ""))
        draft = _new_pending(session, "event_update", card, target_event_id=ev["id"], patch=patch, summary=patch.get("summary", summary),
                             attendees=attendees, old_summary=summary,
                             when_iso=(patch.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("dateTime"))
        return _prepared(draft)
    except Exception as e:
        return _error(f"could not prepare the event change ({type(e).__name__}: {e})", "try again")


# --------------------------------------------------------------------------- calendar: free time (read-only)


@logged
def find_free_time(date="", duration_minutes=30, days=1, day_start=WORK_START, day_end=WORK_END) -> str:
    """Free slots of at least duration_minutes inside working hours, from the primary calendar."""
    try:
        tz = base._tz()
        now = base._now()
        first = _parse_day(_str(date), now.date()) or now.date()
        n_days = max(1, min(base._int(days, 1), 7))
        dur = max(5, min(base._int(duration_minutes, 30), 480))
        sh, sm = _hhmm(day_start, WORK_START)
        eh, em = _hhmm(day_end, WORK_END)
        t_min = datetime.combine(first, datetime.min.time(), tz)
        t_max = t_min + timedelta(days=n_days)
        cal = google_auth.get_services()["calendar"]
        resp = cal.events().list(calendarId="primary", timeMin=t_min.isoformat(), timeMax=t_max.isoformat(), singleEvents=True,
                                 orderBy="startTime", maxResults=250).execute(num_retries=2)
        own = base._own_address()
        busy = []
        for ev in resp.get("items", []) or []:
            if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                continue
            if "dateTime" not in (ev.get("start") or {}):
                continue  # all-day events (holidays, OOO notes) do not block hours here
            mine = [a for a in ev.get("attendees", []) or [] if a.get("self") or str(a.get("email", "")).lower() == own]
            if mine and mine[0].get("responseStatus") == "declined":
                continue
            busy.append((datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(tz), datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(tz)))
        busy.sort()
        free = []
        for i in range(n_days):
            d = first + timedelta(days=i)
            ws = datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz)
            we = datetime(d.year, d.month, d.day, eh, em, tzinfo=tz)
            if d == now.date():
                nxt = now + timedelta(minutes=(15 - now.minute % 15) % 15)
                ws = max(ws, nxt.replace(second=0, microsecond=0))
            cursor = ws
            for bs, be in busy:
                if be <= cursor or bs >= we:
                    continue
                if bs - cursor >= timedelta(minutes=dur):
                    free.append((cursor, bs))
                cursor = max(cursor, be)
            if we - cursor >= timedelta(minutes=dur):
                free.append((cursor, we))
        rows = [{"date": a.date().isoformat(), "weekday": a.strftime("%A"), "from": a.strftime("%H:%M"), "to": b.strftime("%H:%M")} for a, b in free]
        out = {"timezone": str(tz), "duration_minutes": dur, "working_hours": f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}",
               "from_date": first.isoformat(), "days": n_days, "free": rows}
        if not rows:
            out["note"] = "No free slot of that length in working hours."
        return _json(out)
    except Exception as e:
        return _error(f"could not check free time ({type(e).__name__}: {e})", "try again")


# --------------------------------------------------------------------------- commit (Confirm only)


def _batch_call(gmail, requests: list[tuple[str, object]]) -> dict[str, Exception | None]:
    """Run many requests in Gmail batches; returns {key: None on success, else the exception}."""
    results: dict[str, Exception | None] = {}

    def collect(request_id, response, exception):
        results[request_id] = exception

    for i in range(0, len(requests), base.BATCH_MAX):
        batch = gmail.new_batch_http_request(callback=collect)
        for key, req in requests[i : i + base.BATCH_MAX]:
            batch.add(req, request_id=key)
        batch.execute()
    return results


def _commit_email(draft: dict, variant: str) -> dict:
    gmail = google_auth.get_services()["gmail"]
    attempts = int(draft.get("send_attempts") or 0)
    if attempts and variant != "save_draft":
        # A send is not idempotent: never risk a second copy after an unclear first attempt.
        raise RuntimeError("an earlier send attempt for this card did not finish cleanly; check your Sent folder, then ask me to draft it again")
    if draft.get("gmail_draft_id"):
        # The saved-draft send card (draft_send_saved): send exactly the draft already in Gmail.
        if variant == "save_draft":
            raise RuntimeError("this email is already saved in your Gmail drafts; send it, or leave it there")
        draft["send_attempts"] = attempts + 1
        sent = gmail.users().drafts().send(userId="me", body={"id": draft["gmail_draft_id"]}).execute()  # deliberately no retries
        draft["sent_id"] = sent.get("id")
        try:
            got = gmail.users().messages().get(userId="me", id=sent.get("id"), format="minimal").execute(num_retries=2)
            ok = "SENT" in (got.get("labelIds") or [])
        except Exception:
            ok = False
        return {"status": "SENT", "verified": ok, "message_id": sent.get("id"), "thread_id": sent.get("threadId"),
                "report": [f"Sent{' and verified in your Sent folder' if ok else ' (could not read it back from Sent; check there)'}: {draft['subject']}",
                           f"To: {', '.join(draft['to'])}" + (f"  Cc: {', '.join(draft['cc'])}" if draft.get("cc") else "")]}
    msg = EmailMessage()
    msg["From"] = base._own_address()
    msg["To"] = ", ".join(draft["to"])
    if draft.get("cc"):
        msg["Cc"] = ", ".join(draft["cc"])
    msg["Subject"] = draft["subject"]
    if draft.get("in_reply_to"):
        msg["In-Reply-To"] = draft["in_reply_to"]
        msg["References"] = draft.get("references") or draft["in_reply_to"]
    msg.set_content(draft["body"])
    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if draft.get("thread_id"):
        payload["threadId"] = draft["thread_id"]
    if variant == "save_draft":
        made = gmail.users().drafts().create(userId="me", body={"message": payload}).execute()
        mid = (made.get("message") or {}).get("id")
        got = gmail.users().messages().get(userId="me", id=mid, format="minimal").execute(num_retries=2) if mid else {}
        ok = "DRAFT" in (got.get("labelIds") or [])
        return {"status": "SAVED_DRAFT", "verified": ok, "gmail_draft_id": made.get("id"), "message_id": mid,
                "report": [f"Saved to your Gmail drafts{' (verified)' if ok else ''}: {draft['subject']}",
                           f"To: {', '.join(draft['to'])}", "Nothing was sent; open Gmail Drafts to edit or send it."]}
    draft["send_attempts"] = attempts + 1
    sent = gmail.users().messages().send(userId="me", body=payload).execute()  # deliberately no retries
    draft["sent_id"] = sent.get("id")
    try:
        got = gmail.users().messages().get(userId="me", id=sent.get("id"), format="minimal").execute(num_retries=2)
        ok = "SENT" in (got.get("labelIds") or [])
    except Exception:
        ok = False
    return {"status": "SENT", "verified": ok, "message_id": sent.get("id"), "thread_id": sent.get("threadId"),
            "report": [f"Sent{' and verified in your Sent folder' if ok else ' (could not read it back from Sent; check there)'}: {draft['subject']}",
                       f"To: {', '.join(draft['to'])}" + (f"  Cc: {', '.join(draft['cc'])}" if draft.get("cc") else "")]}


def _commit_modify(draft: dict) -> dict:
    gmail = google_auth.get_services()["gmail"]
    act, ids = draft["action"], draft["ids"]
    if act in _LABEL_OPS:
        add, rem = _LABEL_OPS[act]
        gmail.users().messages().batchModify(userId="me", body={"ids": ids, "addLabelIds": add, "removeLabelIds": rem}).execute(num_retries=2)
    elif act in ("label", "unlabel"):
        lid = _label_id(gmail, draft["label_name"])
        if lid is None and act == "label":
            lid = gmail.users().labels().create(userId="me", body={"name": draft["label_name"], "labelListVisibility": "labelShow",
                                                                  "messageListVisibility": "show"}).execute().get("id")
        if lid is None:
            raise RuntimeError(f"there is no label called {draft['label_name']!r}")
        body = {"ids": ids, "addLabelIds": [lid] if act == "label" else [], "removeLabelIds": [lid] if act == "unlabel" else []}
        gmail.users().messages().batchModify(userId="me", body=body).execute(num_retries=2)
        draft["label_id"] = lid
    elif act in ("trash", "untrash"):
        msgs = gmail.users().messages()
        reqs = [(mid, msgs.trash(userId="me", id=mid) if act == "trash" else msgs.untrash(userId="me", id=mid)) for mid in ids]
        _batch_call(gmail, reqs)
    # Verify by reading the labels back.
    meta = base._batch_get(gmail, ids, ["Subject"])
    def done(m: dict) -> bool:
        labels = set(m.get("labelIds") or [])
        return {
            "archive": "INBOX" not in labels, "unarchive": "INBOX" in labels, "mark_read": "UNREAD" not in labels,
            "mark_unread": "UNREAD" in labels, "star": "STARRED" in labels, "unstar": "STARRED" not in labels,
            "trash": "TRASH" in labels, "untrash": "TRASH" not in labels,
            "label": draft.get("label_id") in labels, "unlabel": draft.get("label_id") not in labels,
        }[act]
    ok = sum(1 for mid in ids if mid in meta and done(meta[mid]))
    what = _DONE[act] + (f" '{draft['label_name']}' on" if act == "label" else "")
    report = [f"{what} {ok} of {len(ids)} email(s), verified in Gmail."]
    if act == "trash":
        report.append("They stay in Trash for 30 days; ask me to restore them if needed.")
    return {"status": "DONE", "verified": ok == len(ids), "verified_count": ok, "count": len(ids), "report": report}


def _commit_event(draft: dict) -> dict:
    cal = google_auth.get_services()["calendar"]
    tz = base._tz()
    eid = draft["target_event_id"]  # never "event_id": that key means "already created" for event_create cards
    kind = draft["kind"]
    if kind == "event_delete":
        try:
            cal.events().delete(calendarId="primary", eventId=eid, sendUpdates="none").execute(num_retries=2)
        except Exception as e:
            if base._http_status(e) not in (404, 410):  # 410: already gone (a retry), which is the goal
                raise
        try:
            got = cal.events().get(calendarId="primary", eventId=eid).execute(num_retries=2)
            ok = got.get("status") == "cancelled"
        except Exception as e:
            ok = base._http_status(e) in (404, 410)
        return {"status": "DONE", "verified": ok,
                "report": [f"Deleted{' and verified' if ok else ''}: {draft.get('summary')}", "It is in Google Calendar's trash; no guest was emailed."]}
    if kind == "event_rsvp":
        ev = cal.events().get(calendarId="primary", eventId=eid).execute(num_retries=2)
        own = base._own_address()
        attendees = ev.get("attendees", []) or []
        for a in attendees:
            if a.get("self") or str(a.get("email", "")).lower() == own:
                a["responseStatus"] = draft["response"]
        cal.events().patch(calendarId="primary", eventId=eid, body={"attendees": attendees}, sendUpdates="none").execute(num_retries=2)
        got = cal.events().get(calendarId="primary", eventId=eid).execute(num_retries=2)
        mine = [a for a in got.get("attendees", []) or [] if a.get("self") or str(a.get("email", "")).lower() == own]
        ok = bool(mine) and mine[0].get("responseStatus") == draft["response"]
        return {"status": "DONE", "verified": ok, "htmlLink": got.get("htmlLink"),
                "report": [f"Answered {draft['response']}{' (verified)' if ok else ''}: {got.get('summary')}"]}
    # event_update
    cal.events().patch(calendarId="primary", eventId=eid, body=draft["patch"], sendUpdates="none").execute(num_retries=2)
    got = cal.events().get(calendarId="primary", eventId=eid).execute(num_retries=2)
    patch = draft["patch"]
    ok = all([
        "summary" not in patch or got.get("summary") == patch["summary"],
        "start" not in patch or datetime.fromisoformat(got["start"]["dateTime"]) == datetime.fromisoformat(patch["start"]["dateTime"]),
    ])
    return {"status": "DONE", "verified": ok, "htmlLink": got.get("htmlLink"),
            "report": [f"Updated{' and verified from Google Calendar' if ok else ''}: {got.get('summary')}",
                       f"When: {_event_when(got, tz)}", "No guest was emailed."]}


def block_send() -> bool:
    """VAANI_BLOCK_SEND=1 (read at call time): commit_pending refuses every real send; saving to drafts still works."""
    return os.environ.get("VAANI_BLOCK_SEND", "0").strip().lower() in ("1", "true", "yes", "on")


COMMIT_KINDS = ("email_send", "email_modify", "event_create", "event_update", "event_delete", "event_rsvp")


@logged
def commit_pending(draft_id="", variant="") -> str:
    """Carry out a confirmed card: the one write path for every card, new events included.
    Never offered to the model; only the Confirm click (or Auto, through the same click path)
    calls it. A new event (kind event_create; draft_calendar_event's drafts carry no kind)
    keeps its own state keys, event_id / created / status CREATED, exactly as before."""
    try:
        session = _session()
        if session is None:
            return _error("no active session", "actions can only run inside a conversation turn")
        did = _str(draft_id)
        draft = session.drafts.get(did)
        if draft is None:
            return _error(f"no card with id {did!r} exists in this conversation", "nothing to confirm")
        kind = draft.get("kind") or "event_create"
        if draft.get("committed"):
            out = dict(draft["committed"])
            out["note"] = "This card was already carried out; nothing new was done."
            return _json(out)
        if kind == "event_create" and draft.get("event_id"):
            return base._already_created(draft)  # idempotent: never create twice for one draft
        if draft.get("status") != "DRAFT":
            return _error(f"card {did} is {str(draft.get('status')).lower()}", "ask the user to confirm the latest card")
        if kind == "email_send" and _str(variant) != "save_draft" and block_send():
            return "ERROR: sending is switched off on this server (VAANI_BLOCK_SEND=1); nothing was sent"
        if getattr(session, "confirmed_draft_id", None) != did:
            return f"NOT DONE: waiting for the user to confirm card {did}"
        session.confirmed_draft_id = None  # one click authorises one attempt
        if kind not in COMMIT_KINDS:
            return _error(f"card kind {kind!r} cannot be carried out here", "ask the user to draft it again")
        # This call's log row is the decision row: who confirmed, was it edited, did Google confirm it.
        # Nothing it calls is @logged or annotates, so each write has exactly one decision row.
        decision = autonomy.commit_fields(draft, _str(variant))
        draft["confirmed_by"] = decision["confirmed_by"]
        obs.annotate(**decision, verified=False)
        if decision["confirmed_by"] == "voice":
            heard = obs.current_voice.get() or {}
            try:
                conf = round(float(heard.get("confidence")), 2)
            except (TypeError, ValueError):
                conf = None
            obs.annotate(heard=str(heard.get("heard") or "")[:80], stt_conf=conf)
        if kind == "event_create":
            try:
                return base._insert_confirmed_event(session, draft)
            except Exception as e:
                return _error(f"could not create the event ({type(e).__name__}: {e})", "check Google Calendar and try confirming again")
            finally:
                written, verified = base.event_create_outcome(draft)
                obs.annotate(verified=verified)
                autonomy.on_commit_result(draft, written, verified)
        try:
            if kind == "email_send":
                # Subject and recipients on the log row let the rail's "Drafts saved" list survive a restart.
                obs.annotate(subject=str(draft.get("subject") or "")[:120], to=", ".join(draft.get("to") or [])[:200])
                result = _commit_email(draft, _str(variant))
            elif kind == "email_modify":
                result = _commit_modify(draft)
            else:
                result = _commit_event(draft)
            draft["committed"] = result
            draft["status"] = "DONE"
            if session.pending_draft_id == did:
                session.pending_draft_id = None
        finally:
            done = draft.get("committed")
            verified = autonomy.verified_of(done)
            obs.annotate(verified=verified)
            autonomy.on_commit_result(draft, bool(done), verified)
        return _json(result)
    except Exception as e:
        return _error(f"could not carry out the action ({type(e).__name__}: {e})", "check Gmail or Google Calendar, then try again")


# --------------------------------------------------------------------------- registration

REGISTRY = {
    "draft_email": draft_email,
    "modify_emails": modify_emails,
    "draft_event_change": draft_event_change,
    "find_free_time": find_free_time,
    "commit_pending": commit_pending,  # Confirm path only: deliberately NOT in TOOL_SCHEMAS
}
# draft_nudge and draft_triage are called by code (brief buttons), so they are in neither
# REGISTRY nor TOOL_SCHEMAS: the model's tool list does not change.

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "draft_email", "description": "Prepare an email for the user to send: a new email, a reply (reply_to_message_id) or a forward (forward_message_id). Write the full text yourself. It shows a card; nothing is sent until the user clicks Send.", "parameters": {"type": "object", "properties": {
        "to": {"type": "array", "items": {"type": "string"}, "description": "Email addresses or saved contact names. For a reply it defaults to the original sender."},
        "subject": {"type": "string", "description": "Subject line (ignored for replies and forwards)"},
        "body": {"type": "string", "description": "The complete email text, signed with the user's name"},
        "cc": {"type": "array", "items": {"type": "string"}},
        "reply_to_message_id": {"type": "string", "description": "id of the email being answered, from list_recent_emails"},
        "forward_message_id": {"type": "string", "description": "id of the email being forwarded, from list_recent_emails"}}, "required": ["body"]}}},
    {"type": "function", "function": {"name": "modify_emails", "description": "Prepare a mailbox change: archive, unarchive, mark_read, mark_unread, star, unstar, label, unlabel, trash or untrash. Pick emails by message_ids, by a Gmail query (e.g. from:cloudflare is:unread), or by group (needs_reply, updates, promotions). It shows a card; nothing changes until the user confirms. Trash keeps mail 30 days; mail is never permanently deleted.", "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["archive", "unarchive", "mark_read", "mark_unread", "star", "unstar", "label", "unlabel", "trash", "untrash"]},
        "message_ids": {"type": "array", "items": {"type": "string"}},
        "query": {"type": "string", "description": "Gmail search query"},
        "group": {"type": "string", "enum": ["needs_reply", "updates", "promotions"]},
        "label": {"type": "string", "description": "Label name for label/unlabel"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "draft_event_change", "description": "Prepare a change to an EXISTING calendar event: action=update (move, change length or title), delete, or rsvp (accept/decline/tentative). Find the event by event_id, or by event_query (words from its title or a guest) and on_date. It shows a card; nothing changes until the user confirms.", "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["update", "delete", "rsvp"]},
        "event_id": {"type": "string"},
        "event_query": {"type": "string", "description": "Words from the event title or a guest name, e.g. Andy Chen"},
        "on_date": {"type": "string", "description": "YYYY-MM-DD, today, tomorrow or a weekday"},
        "new_start_iso": {"type": "string", "description": "New start as ISO 8601 local time, e.g. 2026-10-14T17:00"},
        "new_duration_minutes": {"type": "integer"},
        "new_title": {"type": "string"},
        "rsvp": {"type": "string", "enum": ["accept", "decline", "tentative"]}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "find_free_time", "description": "Free slots in working hours (09:00-18:00) on the user's calendar. Read-only.", "parameters": {"type": "object", "properties": {
        "date": {"type": "string", "description": "YYYY-MM-DD, today, tomorrow or a weekday"},
        "duration_minutes": {"type": "integer", "default": 30},
        "days": {"type": "integer", "default": 1, "description": "How many days to search, 1-7"}}}}},
]
