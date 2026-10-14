"""Agent core: Session, TurnResult, run_turn() with the hand-written tool loop.

The system prompt is built once per session from data/memory.json plus
SYSTEM_RULES and frozen at messages[0]; everything after it is append-only.
"""

from __future__ import annotations

import inspect
import email.utils
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from assistant import llm, memory, obs, tools

MAX_ROUNDS = 6
NUDGE_ROUNDS = 2  # one round to call create_calendar_event, one to report it
# CONFIRM_MODE=code (default): a Confirm click runs manage.commit_pending in code (the one
# write path for every card, new events included) and the reply is built from what Google
# stored; the model never decides whether a confirmed write happens. CONFIRM_MODE=model: the
# model is asked to call create_calendar_event itself for a new event (the 7B model skips the
# call on long transcripts, so this is only for comparison).
CONFIRM_MODE = os.environ.get("CONFIRM_MODE", "code").strip().lower()

SYSTEM_RULES = """Rules:
- Never claim to have read email or the calendar without calling a tool.
- To schedule something: resolve the person first. If a name matches two or more contacts, ask one short question which one; do not guess.
- Then call draft_calendar_event and show the draft. Nothing is created until the user clicks Confirm.
- Keep replies short and plain.
- If the requested time is in the past relative to [now], do not call any tool; tell the user it is in the past and ask for a future time.
- Use 30 minutes unless the user says otherwise.
- To write, reply to or forward an email, call draft_email. For a reply or forward, pass the email's id from list_recent_emails. Write the full text yourself and sign it with the user's name. Never write an email out in your reply instead.
- To archive, mark read or unread, star, label or trash email, call modify_emails. Trash keeps mail for 30 days; mail is never permanently deleted.
- To move, rename, delete or answer an existing calendar event, call draft_event_change. To find a free slot, call find_free_time.
- draft_calendar_event, draft_email, modify_emails and draft_event_change only prepare a card. Nothing is sent, changed or deleted until the user clicks the button on the card. Never say it was done; say it is ready to confirm.
- For an inbox overview, call list_recent_emails and group mail by its group field. Never invent mail or senders.
- You cannot search the web, look up news, attach files, or change Gmail settings, filters or forwarding. Say so in one sentence if asked."""

# A 7B model rewrites long base64 event ids when it copies a link, so the link the
# user clicks always comes from the tool result, never from the model's prose.
_GCAL_URL_RE = re.compile(
    r"\[?(https://www\.google\.com/calendar/event\?eid=[^\s\]\)]+)\]?"
    r"(?:\((https://www\.google\.com/calendar/event\?eid=[^\s\]\)]+)\))?"
)
# A sentence that claims a calendar write happened ...
_CLAIMS_CREATED_RE = re.compile(
    r"(?i)\b(?:has been|have been|was|were|is now|is|been|successfully|just)\s+(?:successfully\s+)?"
    r"(?:created|scheduled|added|booked|set up)\b|\bcreated successfully\b"
)
# ... unless it is negated ("nothing was created", "not created", "wasn't created").
_NEGATED_RE = re.compile(r"(?i)\b(?:not|nothing|no|never|n't|wasn't|hasn't|isn't|cannot|can't|couldn't|failed|unable)\b")
_LINK_RESIDUE_RE = re.compile(r"(?im)^\s*(?:html\s*link|link|url)\s*:?\s*$\n?")
# While a draft waits for Confirm, nothing is on the calendar. These catch "is confirmed",
# "will be created shortly", "has been scheduled" and similar claims the model sometimes makes.
_PENDING_CLAIM_RE = re.compile(
    r"(?i)\b(?:is|are|has\s+been|have\s+been|was|were|will\s+be|is\s+now|been|now)\s+(?:successfully\s+|now\s+|being\s+)?"
    r"(?:confirmed|created|scheduled|booked|added\s+to\s+(?:your|the)\s+calendar|set\s+up|sent|forwarded|archived|trashed|deleted|moved|rescheduled|marked\s+as\s+(?:read|unread))\b"
    r"|\bcreated\s+successfully\b|\bevent\s+confirmed\b"
)
PENDING_STATUS_LINE = "Nothing has been created yet: the draft is waiting for you to click Confirm."


def _scrub_pending_claims(reply: str, pending: bool, status_line: str | None = None) -> str:
    """Drop sentences that claim a pending card was already carried out, then state the real status."""
    if not pending or not reply:
        return reply
    dropped = False
    out_lines = []
    for line in reply.split("\n"):
        kept = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if (_PENDING_CLAIM_RE.search(sentence) and not _NEGATED_RE.search(sentence)
                    and "draft" not in sentence.lower()):
                dropped = True
                continue
            kept.append(sentence)
        out_lines.append(" ".join(kept))
    if not dropped:
        return reply
    text = "\n".join(out_lines).strip()
    line = status_line or PENDING_STATUS_LINE
    return f"{text}\n\n{line}" if text else line


def _pin_verified_link(reply: str, link: str) -> str:
    """Replace any calendar event URL (plain or markdown) with the verified link; append it if missing."""
    fixed = _GCAL_URL_RE.sub(link, reply)
    if link not in fixed:
        fixed = f"{fixed.rstrip()}\nLink: {link}"
    return fixed


def _strip_unverified_links(reply: str) -> tuple[str, bool]:
    """Remove every calendar event URL (none was verified this turn). Returns (text, removed_any)."""
    if not _GCAL_URL_RE.search(reply):
        return reply, False
    fixed = _GCAL_URL_RE.sub("", reply)
    fixed = _LINK_RESIDUE_RE.sub("", fixed)
    return fixed.strip(), True


def _claims_created(reply: str) -> bool:
    """True if some sentence says an event was created and that sentence is not negated."""
    for sentence in re.split(r"(?<=[.!?\n])\s+", reply or ""):
        if _CLAIMS_CREATED_RE.search(sentence) and not _NEGATED_RE.search(sentence):
            return True
    return False


LOOP_CAP_REPLY = "I got stuck going round in circles on that one and stopped. Could you rephrase or split the request?"
EMPTY_REPLY = "I ran that but have nothing more to add. Ask me to summarise if you want the details."
NOT_CREATED_REPLY = "Nothing was created: there is no confirmed draft. Confirm the draft first, or ask me to draft it again."


@dataclass
class Session:
    id: str
    messages: list[dict]  # index 0 is the frozen system message; append-only after that
    drafts: dict[str, dict] = field(default_factory=dict)
    pending_draft_id: str | None = None
    confirmed_draft_id: str | None = None
    turn: int = 0
    refused_attendees: list[str] = field(default_factory=list)
    created_at: str = ""


@dataclass
class TurnResult:
    reply: str
    pending_draft: dict | None
    tool_calls: list[dict]  # [{"name", "ms", "ok", "error"}]
    usage: dict  # {"prompt_tokens", "gen_tokens", "model_calls"}
    latency_ms: int
    model: str
    footer: str


# Deliberately tighter than the contract's `^(yes|y|confirm|go ahead|do it|ok)\b`:
# the whole line must be a bare confirmation, so "yes, but make it 4pm" or
# "ok, cancel that" go to the model as text instead of creating the pending draft.
_CONFIRM_RE = re.compile(
    r"^(?:(?:yes|y|yep|yeah|yup|sure|ok|okay|confirm|confirmed|go\s+ahead|do\s+it|send\s+it|please|go)[\s,.!]*){1,4}$",
    re.I,
)


def is_confirmation(text: str) -> bool:
    """True only when the whole input is a bare yes/confirm/go ahead/do it/ok."""
    return bool(_CONFIRM_RE.match((text or "").strip()))


def _tz_name() -> str:
    return memory.load().get("timezone") or "UTC"


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_tz_name())
    except Exception:
        return ZoneInfo("UTC")


_NOW_TAG_RE = re.compile(r"^\[now:[^\]]*\]\s*")


def _now_tag(text: str = "", session: Session | None = None) -> str:
    """[now: 2026-10-13T16:10 Asia/Kolkata], the contract's tag.

    qwen2.5:7b cannot derive a weekday from a date, so when the request (or the
    request this message answers) names a weekday the tag also spells out the
    next seven dates: [now: Tuesday 2026-10-13T16:10 Asia/Kolkata; tomorrow =
    Wednesday 2026-10-14; Thursday = 2026-10-15; ...]. Measured: adding the
    dates to every message makes the model skip the "which Andy?" question, so
    they are added only when needed; draft_calendar_event also checks the named
    weekday against the start it was given and sends the right date back.
    """
    tz = _tz()
    now = datetime.now(tz)
    wants_dates = bool(tools.WEEKDAY_RE.search(text or "")) or (session is not None and tools._requested_weekday(session) is not None)
    if not wants_dates:
        return f"[now: {now:%Y-%m-%dT%H:%M} {tz.key}]"
    days = []
    for i in range(1, 8):
        d = (now + timedelta(days=i)).date()
        days.append(f"tomorrow = {d:%A} {d.isoformat()}" if i == 1 else f"{d:%A} = {d.isoformat()}")
    return f"[now: {now:%A} {now:%Y-%m-%dT%H:%M} {tz.key}; " + "; ".join(days) + "]"


def strip_now_tag(content: str) -> str:
    """The user's own words, without the [now: ...] prefix run_turn adds."""
    return _NOW_TAG_RE.sub("", content or "", count=1)


def build_system_prompt(data: dict | None = None) -> str:
    data = data or memory.load()
    return (
        f"You are Vaani, a personal assistant for {data.get('name') or 'the user'}.\n"
        f"{memory.render_memory_block(data)}\n"
        "Each user message starts with [now: <local time> <timezone>]; use it to resolve today, tomorrow and yesterday.\n"
        f"{SYSTEM_RULES}"
    )


def new_session() -> Session:
    """A fresh session whose system prompt is built ONCE from memory.json."""
    data = memory.load()
    return Session(
        id=secrets.token_hex(4),
        messages=[{"role": "system", "content": build_system_prompt(data)}],
        drafts={},
        pending_draft_id=None,
        confirmed_draft_id=None,
        turn=0,
        created_at=datetime.now(_tz()).isoformat(timespec="seconds"),
    )


def footer_for(n_tools: int, latency_ms: int, prompt_tokens: int, gen_tokens: int) -> str:
    latency_s = latency_ms / 1000
    return f"{n_tools} tool call(s) · {latency_s:.1f}s · {prompt_tokens/1000:.1f}k prompt / {gen_tokens} gen · local"


def pending_draft(session: Session) -> dict | None:
    did = session.pending_draft_id
    if not did:
        return None
    draft = session.drafts.get(did)
    if not draft or draft.get("status") != "DRAFT":
        return None
    out = dict(draft)
    out.setdefault("kind", "event_create")
    if "card" not in out:
        out["card"] = _event_create_card(out)
    return out


def _event_create_card(d: dict) -> dict:
    try:
        when = tools._when(datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"]))
    except (KeyError, ValueError):
        when = f"{d.get('start')} to {d.get('end')}"
    att = d.get("attendees") or []
    note = "Nothing has been written yet. No invite email will be sent"
    note += "; guests may still see the event on their own calendar." if att else "."
    return {"title": f"New event: {d.get('summary')}", "lines": [f"When: {when}", f"Guests: {', '.join(att) if att else 'none'}"],
            "body": d.get("description") or None, "confirm_label": "Confirm", "alt": None, "danger": False, "note": note}


def _status_line(draft: dict | None) -> str:
    kind = (draft or {}).get("kind", "event_create")
    if kind == "event_create":
        return PENDING_STATUS_LINE
    if kind == "email_send":
        return "Nothing has been sent yet: the email is waiting for you to click Send."
    label = ((draft or {}).get("card") or {}).get("confirm_label") or "Confirm"
    return f"Nothing has changed yet: it is waiting for you to click {label}."


def _result(session: Session, reply: str, tool_calls: list[dict], usage: dict, t0: float) -> TurnResult:
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return TurnResult(
        reply=reply,
        pending_draft=pending_draft(session),
        tool_calls=tool_calls,
        usage=usage,
        latency_ms=latency_ms,
        model=llm.MODEL,
        footer=footer_for(len(tool_calls), latency_ms, usage["prompt_tokens"], usage["gen_tokens"]),
    )


def _zero_usage() -> dict:
    return {"prompt_tokens": 0, "gen_tokens": 0, "model_calls": 0}


def _coerce_args(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    try:
        return dict(raw)
    except Exception:
        return {}


def _filter_kwargs(fn, kwargs: dict) -> dict:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _run_tool(name: str, raw_args) -> tuple[str, dict]:
    """Run one registry tool; returns (result string, record for TurnResult.tool_calls)."""
    t0 = time.perf_counter()
    fn = tools.REGISTRY.get(name)
    if fn is None:
        result = f"ERROR: unknown tool {name!r}; nothing was changed; use one of: {', '.join(t['function']['name'] for t in tools.TOOL_SCHEMAS)}"
    else:
        kwargs = _filter_kwargs(fn, _coerce_args(raw_args))
        try:
            result = fn(**kwargs)
        except Exception as e:  # @logged already backstops; this covers bad kwargs
            result = f"ERROR: {name} could not run ({type(e).__name__}: {e}); nothing was changed; try again"
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, default=str)
    ms = int((time.perf_counter() - t0) * 1000)
    return result, {"name": name, "ms": ms, "ok": obs.result_ok(result), "error": obs.error_text(result)}


def _draft_link(draft: dict | None) -> str:
    return str(((draft or {}).get("created") or {}).get("htmlLink") or "")


def _cancel(session: Session, draft_id: str, t0: float) -> TurnResult:
    draft = session.drafts.get(draft_id)
    if session.confirmed_draft_id == draft_id:
        session.confirmed_draft_id = None
    if draft and draft.get("event_id"):
        # A late Cancel after a Confirm: the event exists and this does not delete it.
        link = _draft_link(draft)
        return _result(session, f"That draft was already created; cancelling does not delete it. {link}".strip(), [], _zero_usage(), t0)
    if draft and draft.get("committed"):
        return _result(session, "That was already carried out; cancelling does not undo it.", [], _zero_usage(), t0)
    if draft and draft.get("status") == "DRAFT":
        draft["status"] = "CANCELLED"
    if session.pending_draft_id == draft_id:
        session.pending_draft_id = None
    # Keep the transcript coherent so the model never re-offers a cancelled draft.
    kind = (draft or {}).get("kind", "event_create")
    done = {"event_create": "OK, nothing was created.", "email_send": "OK, nothing was sent."}.get(kind, "OK, nothing was changed.")
    session.messages.append({"role": "user", "content": f"{_now_tag()} User clicked Cancel on card {draft_id}. Do not do it."})
    session.messages.append({"role": "assistant", "content": done})
    return _result(session, done, [], _zero_usage(), t0)


class _Loop:
    """State shared by the model rounds of one turn."""

    def __init__(self, session: Session):
        self.session = session
        self.usage = _zero_usage()
        self.tool_records: list[dict] = []
        self.created_link: str | None = None
        self.create_ok = False
        self.create_failed = False
        self.final_idx: int | None = None  # index in session.messages of the reply the user sees
        self.email_hint: tuple[str, str] | None = None  # ("reply"|"forward", message id) found in code

    def run(self, max_rounds: int) -> str | None:
        """Model rounds until a reply with no tool calls; None when the cap is hit."""
        session = self.session
        for _round in range(max_rounds):
            t_call = time.perf_counter()
            msgs = session.messages
            if obs.current_mode.get() == "voice":  # voice mode: a shorter transcript, faster answers
                from assistant import voicectx

                msgs = voicectx.trim(msgs)
            resp = llm.complete(msgs, tools.TOOL_SCHEMAS)
            call_ms = int((time.perf_counter() - t_call) * 1000)
            msg = resp.message
            content = (msg.content or "").strip()
            calls = list(msg.tool_calls or [])
            self.usage["prompt_tokens"] += int(resp.prompt_eval_count or 0)
            self.usage["gen_tokens"] += int(resp.eval_count or 0)
            self.usage["model_calls"] += 1
            obs.log_model_call(
                session_id=session.id,
                turn=session.turn,
                model=llm.MODEL,
                prompt_tokens=resp.prompt_eval_count or 0,
                gen_tokens=resp.eval_count or 0,
                latency_ms=call_ms,
                had_tool_calls=bool(calls),
            )
            assistant_msg: dict = {"role": "assistant", "content": content}
            call_args = [self._with_email_hint(c.function.name, _coerce_args(c.function.arguments)) for c in calls]
            if calls:
                assistant_msg["tool_calls"] = [
                    {"function": {"name": c.function.name, "arguments": a}} for c, a in zip(calls, call_args)
                ]
            session.messages.append(assistant_msg)
            if not calls:
                self.final_idx = len(session.messages) - 1
                return content or EMPTY_REPLY
            for c, a in zip(calls, call_args):
                result, record = _run_tool(c.function.name, a)
                session.messages.append({"role": "tool", "content": result, "tool_name": c.function.name})
                self.tool_records.append(record)
                if c.function.name == "create_calendar_event":
                    if record["ok"]:
                        self.create_ok = True
                        try:
                            self.created_link = json.loads(result).get("htmlLink") or self.created_link
                        except ValueError:
                            pass
                    else:
                        self.create_failed = True
        return None

    def _with_email_hint(self, name: str, args: dict) -> dict:
        """The user asked to reply to / forward a specific email that code already found: make
        sure draft_email answers THAT email even if the model forgot the id."""
        if name != "draft_email" or not self.email_hint:
            return args
        if args.get("reply_to_message_id") or args.get("forward_message_id"):
            return args
        kind, mid = self.email_hint
        return {**args, ("reply_to_message_id" if kind == "reply" else "forward_message_id"): mid}

    def finish(self, reply: str | None) -> str:
        """Append the loop-cap line when needed and return the reply text."""
        if reply is None:
            reply = LOOP_CAP_REPLY
            self.session.messages.append({"role": "assistant", "content": reply})
            self.final_idx = len(self.session.messages) - 1
        return reply

    def set_reply(self, reply: str) -> None:
        """Make the transcript say what the user actually saw."""
        if self.final_idx is not None and 0 <= self.final_idx < len(self.session.messages):
            self.session.messages[self.final_idx]["content"] = reply


def _created_this_turn(session: Session, pre_created: set[str]) -> list[dict]:
    return [d for did, d in session.drafts.items() if d.get("event_id") and did not in pre_created]


def _trim_dangling(session: Session, pre_len: int) -> None:
    """Drop a trailing assistant message whose tool_calls have no (complete) tool results."""
    msgs = session.messages
    i = len(msgs) - 1
    while i >= pre_len and msgs[i].get("role") == "tool":
        i -= 1
    if i >= pre_len and msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
        if len(msgs) - 1 - i < len(msgs[i]["tool_calls"]):
            del msgs[i:]


def _rollback(session: Session, pre_len: int, pre_drafts: set[str], pre_pending: str | None) -> None:
    """Undo everything this turn appended: transcript, new drafts, pending pointer."""
    del session.messages[pre_len:]
    for did in [d for d in session.drafts if d not in pre_drafts]:
        if not (session.drafts[did].get("event_id") or session.drafts[did].get("committed")):
            del session.drafts[did]
    prev = session.drafts.get(pre_pending) if pre_pending else None
    if prev is not None and prev.get("status") == "SUPERSEDED":
        prev["status"] = "DRAFT"
    session.pending_draft_id = pre_pending if prev is not None and prev.get("status") == "DRAFT" else None


# ---------------------------------------------------------------------------
# Capability guard: requests the assistant has no tool for are answered in code,
# before the model runs, so a 7B model never gets the chance to improvise an
# email or invent a web answer. Patterns only match a request made TO the
# assistant ("can you send...", "reply to...", "search the web..."), not
# questions about received mail ("did Andy send me...", "do I need to reply").
# ---------------------------------------------------------------------------
_REQ = (
    r"(?:^|[.!?]\s+|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?|\bplease\s+"
    r"|\bi\s+(?:want|need|would\s+like)\s+you\s+to\s+|\bhelp\s+me\s+(?:to\s+)?|\bgo\s+ahead\s+and\s+)"
)
_SEND_RE = re.compile(
    _REQ + r"(?:"
    r"send\b(?![^.?!]{0,30}\binvit)"                                   # send ... (not "send an invite")
    r"|(?:reply|respond)\b"                                            # reply / respond (to ...)
    r"|forward\b"
    r"|(?:compose|write|draft)\s+(?:an?\s+|the\s+|a\s+quick\s+)?(?:e-?mail|mail|message|reply|response|note)\b"
    r"|e-?mail\s+(?:\S+@\S+|him|her|them|me|[a-z]+\s+(?:about|regarding|re|that|to\s+say|saying))"
    r"|e-?mail\s+(?!(?:from|updates?|summary|summaries|inbox|digest|list|threads?)\b)[a-z][a-z.'-]*(?:\s+[a-z][a-z.'-]*)?\s*[.?!]*$"  # "email Andy", "email Andy Chen"
    r"|(?:message|text|ping|dm)\s+(?!(?:from|me\s+a\s+summary)\b)[a-z]"   # "message Ravi ..."
    r"|tell\s+(?!(?:me|us)\b)[a-z]+\s+(?:that\b|i\b|i'|we\b|we'|about\b|to\b)"  # "tell Ravi I will ..."
    r"|let\s+(?!(?:me|us)\b)[a-z]+(?:\s+[a-z]+)?\s+know\b"            # "let Andy know ..."
    r"|get\s+back\s+to\s+(?!(?:me|us|work|it|that|this)\b)[a-z]"      # "get back to Cloudflare"
    r")",
    re.I,
)
_WEB_RE = re.compile(
    r"\b(?:search|look\s*up|google|browse|check)\b[^.?!]{0,40}\b(?:web|internet|online)\b"
    r"|(?:^|you\s+|please\s+)google\s+\S"
    r"|\b(?:latest|recent|today'?s|current)\s+news\b"
    r"|\bnews\s+(?:about|on|regarding|for)\b"
    r"|\bwhat'?s\s+new\s+(?:with|about|on)\b",
    re.I,
)
_CAN_DO = ("I can brief your inbox; read, write, reply to and forward email; archive, label or trash mail; "
           "and create, move, delete or answer calendar events, each only after you confirm.")
_PERMA_DELETE_RE = re.compile(
    r"(?i)\b(?:permanently|forever|for\s+good)\b[^.?!]{0,20}\bdelet|\bdelet\w*\b[^.?!]{0,40}\b(?:permanently|forever|for\s+good)\b"
    r"|\bempty\s+(?:the\s+|my\s+)?(?:trash|bin|spam)\b"
)
_SETTINGS_RE = re.compile(
    _REQ + r"(?:create|set\s+up|add|make|change|turn\s+on|turn\s+off|enable|disable|update|edit)\b[^.?!]{0,40}"
    r"\b(?:filters?|forwarding|auto-?reply|auto-?responder|vacation\s+(?:reply|responder)|out[\s-]of[\s-]office|signature|gmail\s+settings)\b",
    re.I,
)
_ATTACH_RE = re.compile(r"(?i)\b(?:attach|attachments?|attached)\b")
PERMA_DELETE_REFUSAL = "I never permanently delete mail. I can move it to Trash, where Gmail keeps it for 30 days."
SETTINGS_REFUSAL = "I can't change Gmail settings, filters, forwarding, signatures or auto-replies; do that in Gmail's settings."
ATTACH_REFUSAL = ("I can't attach files yet. I can draft the email text and save it to your Gmail drafts, "
                  "then you can add the file in Gmail before sending.")
WEB_REFUSAL = "I can't search the web or look up news; I only see your Gmail and Google Calendar."

_SUBJECT_LINE_RE = re.compile(r"(?im)^\s*subject\s*:")
_GREETING_RE = re.compile(r"(?im)^\s*(?:dear|hi|hello|hey)\s+[^\n]{1,40},\s*$")
_SIGNOFF_RE = re.compile(r"(?im)^\s*(?:best|regards|best regards|kind regards|thanks|thank you|cheers|sincerely)[^\n]{0,20},?\s*$")
PROSE_EMAIL_NOTE = ("(That text is only in the chat: nothing was drafted or sent. Ask me to draft it as an email "
                    "to get a Send button.)")


def looks_like_prose_email(reply: str) -> bool:
    """The model wrote an email out in the chat instead of calling draft_email."""
    r = reply or ""
    return bool(_SUBJECT_LINE_RE.search(r)) or bool(_GREETING_RE.search(r) and _SIGNOFF_RE.search(r))


_PAST_DAY_RE = re.compile(r"(?i)\b(?:yesterday|last\s+(?:night|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b")
PAST_REPLY = "That's in the past, so I can't book it. When would you like it instead?"


def past_request_reply(text: str) -> str | None:
    """A scheduling request for yesterday / last week gets a fixed answer (the draft validator
    would refuse it anyway; this keeps the reply clear instead of a muddled question)."""
    t = " ".join(str(text or "").split())
    if _PAST_DAY_RE.search(t) and tools._SCHEDULE_INTENT_RE.search(t):
        return PAST_REPLY
    return None


def capability_refusal(text: str, *, editing_draft: bool = False) -> str | None:
    """A fixed, honest reply for requests this assistant has no tool for; else None.
    Sending, replying, forwarding and changing events now go to the model (behind Confirm)."""
    t = " ".join(str(text or "").split())
    if _PERMA_DELETE_RE.search(t):
        return PERMA_DELETE_REFUSAL
    if _SETTINGS_RE.search(t):
        return SETTINGS_REFUSAL
    send = bool(_SEND_RE.search(t))
    if _WEB_RE.search(t):
        return WEB_REFUSAL + (" I can draft the email if you tell me what it should say." if send else "")
    if send and _ATTACH_RE.search(t):
        return ATTACH_REFUSAL
    return None


def _guarded_reply(session: Session, user_text: str, reply: str, t0: float, label: str = "capability guard") -> TurnResult:
    """Answer without a model call and record the exchange so later turns see it (append-only)."""
    session.messages.append({"role": "user", "content": f"{_now_tag(user_text, session)} {user_text}"})
    session.messages.append({"role": "assistant", "content": reply})
    res = _result(session, reply, [], _zero_usage(), t0)
    res.footer = f"{res.footer} · {label}, no model call"
    return res


# ---------------------------------------------------------------------------
# Inbox brief built in code: the tool fetches and classifies, this formats. The
# 7B model drifted between formats and miscounted unread mail; code does not.
# The model is still used for follow-ups ("what does Ravi want?"), which read
# the tool result left in the transcript and call read_email.
# ---------------------------------------------------------------------------
BRIEF_QUERY = "in:inbox newer_than:3d"
BRIEF_MAX = 20
_BRIEF_RE = re.compile(
    r"(?i)\b(?:check|show|brief|summari[sz]e|scan|review|go\s+through|what'?s\s+(?:new\s+)?in|anything\s+(?:new\s+)?in|update\s+me\s+on|catch\s+me\s+up\s+on)\b"
    r"[^.?!]{0,30}\b(?:e-?mails?|inbox|mail(?:box)?)\b"
    r"|\b(?:latest|new|recent|unread)\s+(?:e-?mails?|mails?|messages)\b"
    r"|\bwhich\s+(?:e-?mails?|mails?|messages)\s+need\b|\bneeds?\s+a\s+reply\b|\bneed\s+to\s+reply\b"
    r"|^\s*(?:my\s+)?inbox\s*[?.!]*\s*$"
)
_BRIEF_EXCLUDE_RE = re.compile(
    r"(?i)\bfrom\s+(?!(?:the\s+)?(?:last|past|this|today|yesterday)\b)[a-z]|\babout\b|\bregarding\b|\bwhat\s+(?:does|did)\b"
)
_GROUP_TITLES = {"needs_reply": "Needs a reply", "updates": "Updates", "promotions": "Promotions"}
_GROUP_LIMITS = {"needs_reply": 8, "updates": 5, "promotions": 4}


def is_inbox_brief_request(text: str) -> bool:
    t = " ".join(str(text or "").split())
    return bool(_BRIEF_RE.search(t)) and not _BRIEF_EXCLUDE_RE.search(t)


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


def format_inbox_brief(data: dict) -> str:
    rows = data.get("messages") or []
    if not rows:
        return "Nothing in your inbox from the last 3 days."
    unread = f"{data.get('unread_count', 0)}{'+' if data.get('unread_count_note') == '100+' else ''}"
    head = f"Inbox, last 3 days: {unread} unread."
    if data.get("more_available"):
        head += f" Showing the newest {len(rows)}."
    parts = [head]
    first_person = ""
    for group in ("needs_reply", "updates", "promotions"):
        entries: dict[tuple[str, str], dict] = {}
        for r in rows:
            if r.get("group", "updates") != group:
                continue
            sender = _tidy(r.get("sender") or r.get("from") or "?")
            key = (sender, _clean_subject(sender, r.get("subject", "")))
            e = entries.setdefault(key, {"n": 0, "unread": False, "date": r.get("date", "")})
            e["n"] += 1
            e["unread"] = e["unread"] or bool(r.get("unread"))
        title = _GROUP_TITLES[group]
        if not entries:
            if group == "needs_reply":
                parts.append(f"**{title}**\nNothing from a person in the last 3 days.")
            continue
        if group == "needs_reply" and not first_person:
            first_person = next(iter(entries))[0].split()[0]
        lines = []
        for i, ((sender, subject), e) in enumerate(list(entries.items())[: _GROUP_LIMITS[group]], 1):
            extra = []
            if e["n"] > 1:
                extra.append(f"x{e['n']}")
            if e["unread"]:
                extra.append("unread")
            if group == "needs_reply" and e["date"]:
                extra.append(e["date"])
            lines.append(f"{i}. {sender} - {subject}" + (f" ({', '.join(extra)})" if extra else ""))
        hidden = len(entries) - _GROUP_LIMITS[group]
        if hidden > 0:
            lines.append(f"... and {hidden} more")
        parts.append(f"**{title}**\n" + "\n".join(lines))
    hint = f'Ask me about any of these, for example "What does {first_person} want?"' if first_person else "Ask me about any of these."
    parts.append(hint)
    return "\n\n".join(parts)


def _inbox_brief_in_code(session: Session, user_text: str, t0: float) -> TurnResult:
    """Fetch, classify and format the brief without a model call. The tool call and its
    result go into the transcript, so follow-up questions can use the message ids."""
    pre_len = len(session.messages)
    token = obs.current_session.set(session)
    try:
        args = {"max_results": BRIEF_MAX, "query": BRIEF_QUERY}
        session.messages.append({"role": "user", "content": f"{_now_tag(user_text, session)} {user_text}"})
        session.messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_recent_emails", "arguments": args}}]})
        result, record = _run_tool("list_recent_emails", args)
        session.messages.append({"role": "tool", "content": result, "tool_name": "list_recent_emails"})
        if record["ok"]:
            reply = format_inbox_brief(json.loads(result))
        else:
            reply = f"I couldn't read your inbox just now: {record.get('error') or result} Nothing was changed; try again in a moment."
        session.messages.append({"role": "assistant", "content": reply})
        res = _result(session, reply, [record], _zero_usage(), t0)
        res.footer = f"{res.footer} · inbox brief built in code, no model call"
        return res
    except Exception as e:
        del session.messages[pre_len:]
        return _result(session, f"Something went wrong reading your inbox ({type(e).__name__}: {e}). Nothing was changed; please try again.", [], _zero_usage(), t0)
    finally:
        obs.current_session.reset(token)


_DAY_BRIEF_RE = re.compile(
    r"(?i)\b(?:brief|plan|summari[sz]e|walk\s+me\s+through)\s+(?:me\s+(?:on|through)\s+)?my\s+day\b"
    r"|\bwhat(?:'s|\s+is|\s+does)\s+my\s+day\b|\bmy\s+day\s+look|\bmorning\s+brief|\bdaily\s+brief"
    r"|\bagenda\s+(?:for\s+)?today\b|\btoday'?s\s+agenda\b|\bstart\s+my\s+day\b"
)


def is_day_brief_request(text: str) -> bool:
    return bool(_DAY_BRIEF_RE.search(" ".join(str(text or "").split())))


def format_day_brief(events: dict, free: dict, inbox: dict) -> str:
    tz = _tz()
    today = datetime.now(tz)
    parts = [f"**Today, {today:%a %d %b}**"]
    rows = events.get("events") or []
    if rows:
        lines = []
        for i, e in enumerate(rows, 1):
            when = "all day" if e.get("all_day") else f"{str(e.get('start', ''))[11:16]}-{str(e.get('end', ''))[11:16]}"
            who = f" with {', '.join(e['attendees'])}" if e.get("attendees") else ""
            lines.append(f"{i}. {when} {e.get('summary', '(no title)')}{who}")
        parts.append("\n".join(lines))
    else:
        parts.append("Nothing on your calendar today.")
    slots = [f"{s['from']}-{s['to']}" for s in (free.get("free") or [])]
    parts.append("**Free today**\n" + (", ".join(slots) if slots else "No free slot left in working hours."))
    parts.append(format_inbox_brief(inbox))
    return "\n\n".join(parts)


def _day_brief_in_code(session: Session, user_text: str, t0: float) -> TurnResult:
    """Today's agenda, free slots and the inbox brief in one reply, without a model call."""
    pre_len = len(session.messages)
    token = obs.current_session.set(session)
    loop = _Loop(session)
    try:
        session.messages.append({"role": "user", "content": f"{_now_tag(user_text, session)} {user_text}"})
        ev_res, ev_rec = _seed_tool_call(session, loop, "list_calendar_events", {"on_date": "today"})
        fr_res, fr_rec = _seed_tool_call(session, loop, "find_free_time", {"date": "today"})
        ib_res, ib_rec = _seed_tool_call(session, loop, "list_recent_emails", {"max_results": BRIEF_MAX, "query": BRIEF_QUERY})
        if not all(r["ok"] for r in (ev_rec, fr_rec, ib_rec)):
            bad = next(r for r in (ev_rec, fr_rec, ib_rec) if not r["ok"])
            reply = f"I couldn't build your day brief just now: {bad.get('error')} Nothing was changed; try again in a moment."
        else:
            reply = format_day_brief(json.loads(ev_res), json.loads(fr_res), json.loads(ib_res))
        session.messages.append({"role": "assistant", "content": reply})
        res = _result(session, reply, loop.tool_records, _zero_usage(), t0)
        res.footer = f"{res.footer} · day brief built in code, no model call"
        return res
    except Exception as e:
        del session.messages[pre_len:]
        return _result(session, f"Something went wrong building your day brief ({type(e).__name__}: {e}). Please try again.", [], _zero_usage(), t0)
    finally:
        obs.current_session.reset(token)


_FOLLOWUP_RE = re.compile(
    r"(?i)\bwhat\s+(?:does|did|do)\s+(?P<a>[a-z][\w'.\-]*(?:\s+[a-z][\w'.\-]*)?)\s+(?:want|say|need|ask|mean|write|send)\b"
    r"|\bwhat'?s\s+(?:the\s+)?(?P<b>[a-z][\w'.\-]*(?:\s+[a-z][\w'.\-]*)?)\s+(?:e-?mail|mail|message)\s+about\b"
    r"|\b(?:read|open|summari[sz]e|show)\s+(?:me\s+)?(?:the\s+)?(?:latest\s+|last\s+)?(?:e-?mail|mail|message)\s+from\s+(?P<c>[a-z][\w'.\-]*(?:\s+[a-z][\w'.\-]*)?)"
    r"|\b(?:reply|respond|answer)\s+to\s+(?:the\s+)?(?P<d>[a-z][\w'.\-]*(?:\s+[a-z][\w'.\-]*)?)"
    r"|\bforward\s+(?:the\s+)?(?:latest\s+|last\s+)?(?:e-?mail|mail|message)\s+from\s+(?P<f>[a-z][\w'.\-]*(?:\s+[a-z][\w'.\-]*)?)"
    r"|\bforward\s+(?:the\s+)?(?P<e>[a-z][\w'.\-]*(?:\s+[a-z][\w'.\-]*)?)(?:'s)?\s+(?:e-?mail|mail|message)"
)
# Words that end a name: "reply to Ravi saying thanks" -> "Ravi".
_NAME_STOP = {"saying", "and", "with", "about", "that", "to", "email", "mail", "message", "thanking", "telling", "asking",
              "for", "re", "regarding", "tell", "say", "ask", "thank", "please", "now", "today", "tomorrow"}
_NOT_A_NAME = {"it", "this", "that", "he", "she", "they", "him", "her", "them", "the", "my", "you", "i", "we", "email", "mail", "message"}


def followup_kind(text: str) -> str | None:
    """'reply' or 'forward' when the request is to answer or forward an email, else None."""
    m = _FOLLOWUP_RE.search(" ".join(str(text or "").split()))
    if not m:
        return None
    if m.group("d"):
        return "reply"
    if m.group("e") or m.group("f"):
        return "forward"
    return None


def followup_sender(text: str) -> str | None:
    """The sender named in "what does Ravi want?" / "read the email from Paula", else None."""
    m = _FOLLOWUP_RE.search(" ".join(str(text or "").split()))
    if not m:
        return None
    name = (m.group("a") or m.group("b") or m.group("c") or m.group("d") or m.group("e") or m.group("f") or "").strip(" '.")
    words = []
    for w in name.split():
        if w.lower().strip("'.,") in _NAME_STOP:
            break
        words.append(w.strip("'.,").removesuffix("'s"))
    name = " ".join(words)
    if not name or name.split()[0].lower() in _NOT_A_NAME:
        return None
    return name


def _rows_from_last_listing(session: Session) -> list[dict] | None:
    for m in reversed(session.messages):
        if m.get("role") == "tool" and m.get("tool_name") == "list_recent_emails":
            try:
                return json.loads(m.get("content") or "{}").get("messages") or []
            except ValueError:
                return None
    return None


def _pick_row(rows: list[dict], name: str) -> dict | None:
    toks = [t for t in name.lower().split() if t]
    hits = [r for r in rows if all(t in f"{r.get('sender', '')} {r.get('from', '')}".lower() for t in toks)]
    hits.sort(key=lambda r: r.get("group") != "needs_reply")  # stable: people first, newest first within
    return hits[0] if hits else None


def _seed_tool_call(session: Session, loop: "_Loop", name: str, args: dict) -> tuple[str, dict]:
    """Run one tool in code and record it in the transcript exactly as if the model had called it."""
    session.messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]})
    result, record = _run_tool(name, args)
    session.messages.append({"role": "tool", "content": result, "tool_name": name})
    loop.tool_records.append(record)
    return result, record


def _seed_email_followup(session: Session, loop: "_Loop", user_text: str) -> None:
    """For "what does Ravi want?": find Ravi's email (in the brief already shown, else a
    Gmail search) and read it in code, so the model only has to summarise the body."""
    name = followup_sender(user_text)
    if not name:
        return
    rows = _rows_from_last_listing(session)
    row = _pick_row(rows, name) if rows else None
    if row is None:
        safe = name.replace('"', "")
        result, record = _seed_tool_call(session, loop, "list_recent_emails", {"max_results": 5, "query": f'from:"{safe}" newer_than:30d'})
        if not record["ok"]:
            return
        try:
            row = _pick_row(json.loads(result).get("messages") or [], name)
        except ValueError:
            row = None
    if row is not None and row.get("id"):
        _seed_tool_call(session, loop, "read_email", {"message_id": row["id"]})
        kind = followup_kind(user_text)
        if kind:
            loop.email_hint = (kind, row["id"])


# ---------------------------------------------------------------------------
# Reply / forward in code: the 7B model, handed the original email, tends to print it
# instead of drafting. So code finds and reads the email, the model writes ONLY the
# reply text in a focused call without tools, and code prepares the card.
# ---------------------------------------------------------------------------
_SAYING_RE = re.compile(
    r"(?i)\b(?:saying|to\s+say|that\s+says|telling\s+(?:him|her|them)|tell\s+(?:him|her|them)|and\s+say|with\s+(?:a\s+)?note|with\s+the\s+message"
    r"|(?:reply|respond|answer|write\s+back)(?:\s+to\s+\S+)?\s+(?:that|with))\s*:?\s+(?P<msg>.+)$"
)
_FWD_TO_RE = re.compile(
    r"(?i)\bto\s+(?P<to>[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*\.\w{2,}|[A-Za-z][\w'.\-]*(?:\s+[A-Za-z][\w'.\-]*)?)(?=\s*(?:$|[.,;!?]|\s+(?:saying|with|and|about|to)\b))"
)
REPLY_WRITER = (
    "You write short, polite, plain-text emails for {name}. Return ONLY the email body: start with a greeting, "
    "end signed '{name}'. Two to four sentences. Say what the user asked for and do not add details, promises or "
    "questions they did not ask for. No subject line, no placeholders like [Name], no notes to the user."
)


def _recipient_first_name(from_header: str) -> str:
    """'Ravi Mehta (Basecamp) <x@y>' -> 'Ravi'; a company sender ('Cloudflare') -> ''."""
    disp, _addr = email.utils.parseaddr(str(from_header or ""))
    m = tools.RELAYED_PERSON_RE.match(disp or "")
    if m:
        return m.group("person").split()[0]
    if tools.AUTOMATED_FROM_RE.search(str(from_header or "")):
        return ""  # no-reply, notifications, mailer-daemon: not a person to greet by name
    words = [w for w in re.split(r"\s+", re.sub(r"\(.*?\)", "", disp or "").strip()) if w]
    return words[0] if len(words) >= 2 and words[0][:1].isupper() and words[1][:1].isupper() else ""


def _instruction(text: str) -> str:
    m = _SAYING_RE.search(" ".join(str(text or "").split()))
    return m.group("msg").strip(" .") if m else ""


def _forward_recipient(text: str) -> str:
    t = " ".join(str(text or "").split())
    name = followup_sender(t) or ""
    i = t.lower().find(name.lower()) if name else -1
    tail = t[i + len(name):] if i >= 0 else t  # search after the sender's name, never inside it
    r = _FWD_TO_RE.search(tail)
    return r.group("to").strip() if r else ""


def _write_email_body(session: Session, loop: "_Loop", kind: str, original: dict, instruction: str) -> str:
    name = str(memory.load().get("name") or "me")
    first = _recipient_first_name(original.get("from"))
    who = first or "the sender"
    if kind == "reply":
        ask = (f"Write a reply to this email.\nFrom: {original.get('from')}\nSubject: {original.get('subject')}\n\n"
               f"{str(original.get('body') or '')[:2000]}\n\n"
               f"What the user wants to tell {who}: {instruction or 'a short, friendly acknowledgement'}\n"
               + (f"Start with 'Hi {first},'. " if first else "Start with a short greeting. ")
               + f"Write to {who} directly: where the user says 'him', 'her' or 'them' about {who}, write 'you'.")
    else:
        ask = (f"Write a one or two sentence note to go on top of a forwarded email (subject: {original.get('subject')}). "
               f"The note should say: {instruction or 'FYI, see the email below'}")
    t_call = time.perf_counter()
    resp = llm.complete([{"role": "system", "content": REPLY_WRITER.format(name=name)}, {"role": "user", "content": ask}], [])
    loop.usage["prompt_tokens"] += int(resp.prompt_eval_count or 0)
    loop.usage["gen_tokens"] += int(resp.eval_count or 0)
    loop.usage["model_calls"] += 1
    obs.log_model_call(session_id=session.id, turn=session.turn, model=llm.MODEL, prompt_tokens=resp.prompt_eval_count or 0,
                       gen_tokens=resp.eval_count or 0, latency_ms=int((time.perf_counter() - t_call) * 1000), had_tool_calls=False)
    body = (resp.message.content or "").strip()
    body = _SUBJECT_LINE_RE.sub("", body, count=1).lstrip() if body.lower().startswith("subject") else body
    if first:  # the 7B sometimes applies "him -> you" to the greeting too
        body = re.sub(r"^(hi|hello|dear)\s+you\b", lambda m: f"{m.group(1)} {first}", body, count=1, flags=re.I)
    return body


# ---------------------------------------------------------------------------
# Conversation context: the email the user is looking at, and what the assistant just asked.
# "reply to him saying ...", "reply saying ...", "forward it to ...", and "yes" to an offer to
# reply all act on the email read most recently in this chat.
# ---------------------------------------------------------------------------
_AFFIRM_PREFIX = r"^(?:(?:yes|yeah|yep|ok|okay|sure)\b[,.!]?\s*)?(?:(?:can|could|would|will)\s+you\s+|please\s+)?"
_CTX_REPLY_RE = re.compile(_AFFIRM_PREFIX + r"(?:reply|respond|answer|write\s+back)\b", re.I)
_CTX_FORWARD_RE = re.compile(_AFFIRM_PREFIX + r"forward\s+(?:it|this|that|the\s+(?:e-?mail|mail|message))\b", re.I)
_OFFER_REPLY_RE = re.compile(r"(?i)\b(?:reply|respond|answer|write\s+back)\b")
_OFFER_FORWARD_RE = re.compile(r"(?i)\bforward\b")
FOCUS_USER_TURNS = 6  # how many of the user's own messages back "the email we were talking about" can be
_SYNTHETIC_USER = ("User clicked", "You did not call", "You wrote the email in the chat")


def _is_synthetic(m: dict) -> bool:
    return strip_now_tag(str(m.get("content") or "")).startswith(_SYNTHETIC_USER)


def _last_assistant_text(session: Session) -> str:
    for m in reversed(session.messages):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            return str(m["content"])
        if m.get("role") == "user" and not _is_synthetic(m):
            return ""  # the user has spoken since the assistant last said anything
    return ""


def _read_result(session: Session, message_id: str) -> dict | None:
    for m in reversed(session.messages):
        if m.get("role") == "tool" and m.get("tool_name") == "read_email":
            try:
                found = json.loads(m.get("content") or "{}")
            except ValueError:
                continue
            if found.get("id") == message_id:
                return found
    return None


def _email_in_focus(session: Session) -> dict | None:
    """The email this chat is about: the one read most recently, or the one a reply/forward card
    was last prepared for, within the user's last few messages (button clicks do not count)."""
    users = 0
    for m in reversed(session.messages):
        if m.get("role") == "user" and not _is_synthetic(m):
            users += 1
            if users > FOCUS_USER_TURNS:
                return None
        if m.get("role") == "tool" and m.get("tool_name") == "read_email":
            try:
                found = json.loads(m.get("content") or "{}")
            except ValueError:
                continue
            if found.get("id"):
                return found
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                f = c.get("function") or {}
                a = f.get("arguments") or {}
                if f.get("name") == "draft_email" and isinstance(a, dict):
                    mid = a.get("reply_to_message_id") or a.get("forward_message_id")
                    if mid:
                        return _read_result(session, mid)
    return None


def contextual_kind(text: str) -> str | None:
    """'reply' / 'forward' for requests that point at the email in focus without naming anyone."""
    t = " ".join(str(text or "").split())
    if followup_sender(t):
        return None  # a named request ("reply to Ravi") goes the named way
    if _CTX_FORWARD_RE.search(t):
        return "forward"
    if _CTX_REPLY_RE.search(t):
        return "reply"
    return None


def _offered(last_assistant: str) -> str | None:
    """What the assistant's last question offered: 'reply', 'forward', 'both' or None."""
    if "?" not in last_assistant:
        return None
    question = last_assistant[last_assistant.rfind("\n", 0, last_assistant.rfind("?")) + 1:]
    r, f = bool(_OFFER_REPLY_RE.search(question)), bool(_OFFER_FORWARD_RE.search(question))
    return "both" if r and f else "reply" if r else "forward" if f else None


def _reply_or_forward_in_code(session: Session, user_text: str, t0: float, *, kind: str | None = None,
                              focus: dict | None = None) -> TurnResult | None:
    """Returns None (let the model handle it) when no matching email is found.
    With `focus`, it acts on that already-read email instead of searching by name."""
    kind = kind or followup_kind(user_text)
    name = None if focus is not None else followup_sender(user_text)
    if not kind or (focus is None and not name):
        return None
    pre_len = len(session.messages)
    pre_drafts, pre_pending = set(session.drafts), session.pending_draft_id
    token = obs.current_session.set(session)
    loop = _Loop(session)
    try:
        session.messages.append({"role": "user", "content": f"{_now_tag(user_text, session)} {user_text}"})
        rows = _rows_from_last_listing(session) if focus is None else None
        row = None
        if focus is not None:
            disp, _addr = email.utils.parseaddr(str(focus.get("from") or ""))
            row = {"id": focus["id"], "from": focus.get("from"), "sender": disp or focus.get("from"), "subject": focus.get("subject")}
        for cand in ([] if focus is not None else dict.fromkeys([name, name.split()[0]])):
            row = _pick_row(rows, cand) if rows else None
            if row is None:
                safe = cand.replace('"', "")
                res, rec = _seed_tool_call(session, loop, "list_recent_emails", {"max_results": 5, "query": f'from:"{safe}" newer_than:30d'})
                if rec["ok"]:
                    row = _pick_row(json.loads(res).get("messages") or [], cand)
            if row is not None:
                break
        if row is None or not row.get("id"):
            del session.messages[pre_len:]
            return None
        if focus is not None:
            original = focus  # already read in this chat; no need to fetch it again
        else:
            res, rec = _seed_tool_call(session, loop, "read_email", {"message_id": row["id"]})
            original = json.loads(res) if rec["ok"] else {"from": row.get("from"), "subject": row.get("subject"), "body": row.get("snippet")}
        body = _write_email_body(session, loop, kind, original, _instruction(user_text))
        args: dict = {"body": body}
        if kind == "reply":
            args["reply_to_message_id"] = row["id"]
        else:
            args["forward_message_id"] = row["id"]
            to = _forward_recipient(user_text)
            if to:
                args["to"] = [to]
        res, rec = _seed_tool_call(session, loop, "draft_email", args)
        sender = " ".join(str(row.get("sender") or row.get("from") or name).split())
        if rec["ok"]:
            what = "reply to" if kind == "reply" else "forward of the email from"
            reply = f"Here is a {what} {sender}, ready for you to check. Nothing is sent until you click Send."
        elif kind == "forward" and "recipient" in str(rec.get("error") or ""):
            reply = f"Who should I forward {sender}'s email to? Type their email address, or the name of a saved contact."
        else:
            reply = f"I couldn't prepare that: {rec.get('error') or res}"
        session.messages.append({"role": "assistant", "content": reply})
        out = _result(session, reply, loop.tool_records, loop.usage, t0)
        out.footer = f"{out.footer} · email found in code, text written by the model"
        return out
    except Exception as e:
        _rollback(session, pre_len, pre_drafts, pre_pending)
        if isinstance(e, llm.LLMError):
            return _result(session, f"I can't reach the local model right now: {e}. Nothing was changed.", [], loop.usage, t0)
        return _result(session, f"Something went wrong preparing that email ({type(e).__name__}: {e}). Nothing was changed.", [], loop.usage, t0)
    finally:
        obs.current_session.reset(token)


def _report_created(created: dict) -> str:
    """Deterministic report of a created event, built only from the tool's read-back."""
    start = (created.get("start") or {}).get("dateTime") or (created.get("start") or {}).get("date") or "?"
    end = (created.get("end") or {}).get("dateTime") or (created.get("end") or {}).get("date") or "?"
    when = created.get("when") or f"{start} to {end}"
    raw_att = created.get("attendees") or []
    att = ", ".join(a if isinstance(a, str) else str((a or {}).get("email") or a) for a in raw_att) or "none"
    head = "Created and verified from Google Calendar" if created.get("status") == "CREATED" else "Created on Google Calendar (read-back not verified yet)"
    lines = [f"{head}: {created.get('summary') or 'event'}", f"When: {when}", f"Attendees: {att} (no invite email was sent)"]
    if created.get("htmlLink"):
        lines.append(f"Link: {created['htmlLink']}")
    if created.get("note"):
        lines.append(str(created["note"]))
    return "\n".join(lines)


def _commit_other_card(session: Session, did: str, t0: float, variant: str) -> TurnResult:
    """Confirm for email and event-change cards: commit_pending runs in code, the reply is built
    from what Google returned, and the call plus its result go into the transcript."""
    draft = session.drafts.get(did) or {}
    label = ((draft.get("card") or {}).get("alt") or {}).get("label") if variant else (draft.get("card") or {}).get("confirm_label")
    pre_len = len(session.messages)
    token = obs.current_session.set(session)
    records: list[dict] = []
    try:
        session.messages.append({"role": "user", "content": f"{_now_tag()} User clicked {label or 'Confirm'} on card {did}."})
        args = {"draft_id": did, "variant": variant or ""}
        session.messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "commit_pending", "arguments": args}}]})
        result, record = _run_tool("commit_pending", args)
        records.append(record)
        session.messages.append({"role": "tool", "content": result, "tool_name": "commit_pending"})
        done = draft.get("committed")
        if record["ok"] and done:
            reply = "\n".join(done.get("report") or ["Done."])
            if done.get("htmlLink"):
                reply += f"\nLink: {done['htmlLink']}"
        elif draft.get("kind") == "email_send" and draft.get("send_attempts"):
            reply = (f"I could not confirm the email was sent: {record.get('error') or result} "
                     "Check your Sent folder before trying again.")
        else:
            reply = f"I could not do that: {record.get('error') or result} Nothing was changed; click {label or 'Confirm'} again to retry."
        session.messages.append({"role": "assistant", "content": reply})
        return _result(session, reply, records, _zero_usage(), t0)
    except Exception as e:
        if draft.get("committed"):
            reply = "It was carried out, but I could not finish the reply: " + "; ".join(draft["committed"].get("report") or [])
        else:
            del session.messages[pre_len:]
            reply = f"Something went wrong on my side ({type(e).__name__}: {e}). Check Gmail or Google Calendar before retrying."
        return _result(session, reply, records, _zero_usage(), t0)
    finally:
        session.confirmed_draft_id = None
        obs.current_session.reset(token)


def _result_link(result: str) -> str:
    """The htmlLink in a tool's JSON result (what Google stored), or ""."""
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return ""
    return str(data.get("htmlLink") or "") if isinstance(data, dict) else ""


def _confirm_in_code(session: Session, did: str, t0: float) -> TurnResult:
    """Confirm of a new event, executed by code, not by the model.

    Runs commit_pending directly, the same write path as every other card (the gate
    is satisfied because the caller set confirmed_draft_id for this draft), builds the
    reply from Google's read-back with the link pinned from the tool result, and
    appends the call and its result to the transcript so later model turns see
    exactly what happened. No model call, so no chance of the model skipping the
    write or inventing a link.
    """
    pre_len = len(session.messages)
    pre_created = {d for d, v in session.drafts.items() if v.get("event_id")}
    token = obs.current_session.set(session)
    records: list[dict] = []
    try:
        session.messages.append({"role": "user", "content": f"{_now_tag()} User clicked Confirm on draft {did}."})
        args = {"draft_id": did}
        session.messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "commit_pending", "arguments": args}}]})
        result, record = _run_tool("commit_pending", args)
        records.append(record)
        session.messages.append({"role": "tool", "content": result, "tool_name": "commit_pending"})
        draft = session.drafts.get(did) or {}
        created = draft.get("created") if draft.get("event_id") else None
        if record["ok"] and created:
            reply = _report_created(created)
            link = _result_link(result)
            if link:
                reply = _pin_verified_link(reply, link)  # the link is Google's, never model prose
        else:
            why = record.get("error") or result
            reply = f"I could not create draft {did}: {why} Nothing was created; confirm it again to retry."
        session.messages.append({"role": "assistant", "content": reply})
        return _result(session, reply, records, _zero_usage(), t0)
    except Exception as e:
        created_now = _created_this_turn(session, pre_created)
        if created_now:
            _trim_dangling(session, pre_len)
            d = created_now[0]
            reply = f"The event WAS created ({d.get('summary')}, {d.get('start')} to {d.get('end')}) but I could not finish the reply ({type(e).__name__}: {e})."
            link = _draft_link(d)
            if link:
                reply = _pin_verified_link(reply, link)
            session.messages.append({"role": "assistant", "content": reply})
            return _result(session, reply, records, _zero_usage(), t0)
        del session.messages[pre_len:]
        return _result(session, f"Something went wrong on my side ({type(e).__name__}: {e}). Nothing was changed; confirm again to retry.", records, _zero_usage(), t0)
    finally:
        session.confirmed_draft_id = None  # single-use
        obs.current_session.reset(token)


def run_turn(
    session: Session,
    text: str | None = None,
    *,
    confirm_draft_id: str | None = None,
    cancel_draft_id: str | None = None,
    confirm_variant: str | None = None,
) -> TurnResult:
    t0 = time.perf_counter()
    session.turn += 1

    if cancel_draft_id is not None:
        return _cancel(session, str(cancel_draft_id), t0)

    did: str | None = None
    attempts_before = 0
    if confirm_draft_id is not None:
        did = str(confirm_draft_id)
        draft = session.drafts.get(did)
        if draft is None:
            return _result(session, f"There is no pending draft with id {did}. Nothing was created.", [], _zero_usage(), t0)
        if draft.get("event_id"):
            return _result(session, f"That draft was already created. Nothing new was created. {_draft_link(draft)}".strip(), [], _zero_usage(), t0)
        if draft.get("committed"):
            return _result(session, "That card was already carried out. Nothing new was done.", [], _zero_usage(), t0)
        if draft.get("status") == "CANCELLED":
            return _result(session, "That draft was cancelled. Nothing was created; ask me to draft it again.", [], _zero_usage(), t0)
        if draft.get("status") != "DRAFT" or session.pending_draft_id != did:
            return _result(session, "That draft was replaced by a newer one. Nothing was created; confirm the latest draft or ask me to draft it again.", [], _zero_usage(), t0)
        attempts_before = int(draft.get("insert_attempts") or 0)
        # The confirmation is scoped to THIS turn: the finally below clears it
        # whatever happens, and the write (commit_pending, or create_calendar_event in
        # CONFIRM_MODE=model) consumes it on first use.
        session.confirmed_draft_id = did
        if draft.get("kind", "event_create") != "event_create":
            return _commit_other_card(session, did, t0, str(confirm_variant or ""))
        if CONFIRM_MODE != "model":
            return _confirm_in_code(session, did, t0)
        text = f"User clicked Confirm on draft {did}. Create it now and report exactly what you read back from Google Calendar."

    if text is None or not str(text).strip():
        return _result(session, "Tell me what you need: inbox updates, your calendar, or something to schedule.", [], _zero_usage(), t0)

    user_text = str(text).strip()
    if did is None:
        refusal = capability_refusal(user_text, editing_draft=bool(session.pending_draft_id))
        if refusal:
            return _guarded_reply(session, user_text, refusal, t0)
        if is_confirmation(user_text):
            card = (pending_draft(session) or {}).get("card")
            if card:
                return _guarded_reply(session, user_text, f"To go ahead, click {card['confirm_label']} on the card above.", t0,
                                      label="no typed confirmations")
            last = _last_assistant_text(session)
            offer = _offered(last)
            focus = _email_in_focus(session)
            if offer == "reply" and focus is not None:
                handled = _reply_or_forward_in_code(session, user_text, t0, kind="reply", focus=focus)
                if handled is not None:
                    return handled
            if "?" not in last:
                return _guarded_reply(session, user_text, "There is no pending draft to confirm. What would you like me to do?", t0,
                                      label="no typed confirmations")
            # Otherwise "yes" answers the assistant's own question: the model has the context.
        past = past_request_reply(user_text)
        if past:
            return _guarded_reply(session, user_text, past, t0, label="past-date check")
        which = tools.ambiguous_contact_question(user_text)
        if which:
            return _guarded_reply(session, user_text, which, t0, label="which-contact check")
        if is_day_brief_request(user_text):
            return _day_brief_in_code(session, user_text, t0)
        if is_inbox_brief_request(user_text):
            return _inbox_brief_in_code(session, user_text, t0)
        if followup_kind(user_text):
            handled = _reply_or_forward_in_code(session, user_text, t0)
            if handled is not None:
                return handled
        ctx = contextual_kind(user_text)
        if ctx:
            focus = _email_in_focus(session)
            if focus is not None:
                handled = _reply_or_forward_in_code(session, user_text, t0, kind=ctx, focus=focus)
                if handled is not None:
                    return handled
    pre_len = len(session.messages)
    pre_drafts = set(session.drafts)
    pre_pending = session.pending_draft_id
    pre_created = {d for d, v in session.drafts.items() if v.get("event_id")}
    loop = _Loop(session)
    token = obs.current_session.set(session)
    try:
        session.messages.append({"role": "user", "content": f"{_now_tag(user_text, session)} {user_text}"})
        if did is None:
            _seed_email_followup(session, loop, user_text)
            pend = pending_draft(session)
            if loop.email_hint is None and pend and pend.get("kind") == "email_send" and pend.get("mode") in ("reply", "forward") \
                    and pend.get("source_message_id"):
                loop.email_hint = (pend["mode"], pend["source_message_id"])
        reply = loop.finish(loop.run(MAX_ROUNDS))

        if did is not None and not loop.create_ok:
            draft = session.drafts[did]
            if draft.get("status") == "DRAFT" and int(draft.get("insert_attempts") or 0) == attempts_before:
                # The 7B model sometimes answers "Creating the event..." without calling
                # the tool. One explicit nudge, then give up with a non-model reply.
                session.messages.append(
                    {"role": "user", "content": f"{_now_tag()} You did not call create_calendar_event. Call create_calendar_event with draft_id {did} now."}
                )
                reply = loop.finish(loop.run(NUDGE_ROUNDS))

        if loop.create_ok:
            if not loop.created_link:
                loop.created_link = _draft_link(session.drafts.get(did or "")) or next(
                    (_draft_link(d) for d in _created_this_turn(session, pre_created)), ""
                ) or None
            if loop.created_link:
                reply = _pin_verified_link(reply, loop.created_link)
        else:
            reply, stripped = _strip_unverified_links(reply)
            if did is not None:
                draft = session.drafts[did]
                failed = next((r["error"] for r in loop.tool_records if r["name"] == "create_calendar_event" and not r["ok"]), None)
                if draft.get("status") == "DRAFT":
                    why = f": {failed}" if failed else ""
                    reply = f"I could not create draft {did}{why}. Nothing was created; confirm it again to retry."
                elif _claims_created(reply):
                    reply = NOT_CREATED_REPLY
            elif _claims_created(reply) and (stripped or loop.create_failed or is_confirmation(user_text)):
                reply = NOT_CREATED_REPLY
            reply = _scrub_pending_claims(reply, bool(session.pending_draft_id), _status_line(pending_draft(session)))
        prose_note = False
        drafted = any(r["name"] == "draft_email" and r["ok"] for r in loop.tool_records)
        if did is None and not drafted and looks_like_prose_email(reply):
            # The model wrote the email in the chat instead of preparing it: one nudge, then say so.
            session.messages.append({"role": "user", "content": f"{_now_tag()} You wrote the email in the chat. Call draft_email with to, subject and body instead, so the user gets a Send button. Do not write the email in your reply."})
            reply = loop.finish(loop.run(NUDGE_ROUNDS))
            drafted = any(r["name"] == "draft_email" and r["ok"] for r in loop.tool_records)
            if not drafted and looks_like_prose_email(reply):
                reply, prose_note = f"{reply.rstrip()}\n\n{PROSE_EMAIL_NOTE}", True
        loop.set_reply(reply)
        res = _result(session, reply, loop.tool_records, loop.usage, t0)
        if prose_note:
            res.footer = f"{res.footer} · email was only written in the chat"
        return res
    except Exception as e:  # LLMError included; never let an agent bug surface as a crash
        created = _created_this_turn(session, pre_created)
        if created:
            # A real write already happened this turn: never say "nothing was changed",
            # keep the tool records, and leave the transcript consistent.
            _trim_dangling(session, pre_len)
            d = created[0]
            link = _draft_link(d)
            reply = (
                f"The event WAS created ({d.get('summary')}, {d.get('start')} to {d.get('end')}) but I could not finish the reply "
                f"({type(e).__name__}: {e})."
            )
            if link:
                reply = _pin_verified_link(reply, link)
            session.messages.append({"role": "assistant", "content": reply})
            return _result(session, reply, loop.tool_records, loop.usage, t0)
        _rollback(session, pre_len, pre_drafts, pre_pending)
        if isinstance(e, llm.LLMError):
            return _result(session, f"I can't reach the local model right now: {e}. Nothing was changed.", [], loop.usage, t0)
        return _result(session, f"Something went wrong on my side ({type(e).__name__}: {e}). Nothing was changed; please try again.", [], loop.usage, t0)
    except BaseException:  # Ctrl-C in the REPL: leave the transcript consistent, then re-raise
        if _created_this_turn(session, pre_created):
            _trim_dangling(session, pre_len)
        else:
            _rollback(session, pre_len, pre_drafts, pre_pending)
        raise
    finally:
        if did is not None:
            session.confirmed_draft_id = None  # single-use: a retry re-arms it via confirm_draft_id
        obs.current_session.reset(token)
