"""Voice approvals (POLICY): tiers, read-back text, the approval checks and the countdown.

Decided in code, never by the model. A spoken word approves a card only when all seven
checks hold (.build/CONTRACT-v3.md, "Approval policy"): voice mode, a fresh single-use
transcript from the Mac's own speech-to-text, no overlap with Vaani's own speech, enough
confidence, the whole utterance is the word, the word belongs to this card, and THIS card
was read back to the end just before. Each failure has one fixed spoken reason, and
nothing is written.

Tier 1 (reversible or stays in the account: save to drafts, archive, an event without
guests) commits at once through flow.confirm(..., confirmer="voice"), the same gate a
click uses. Tier 2 (leaves the account: send, trash, invites, RSVPs, changes guests see)
arms a 5-second countdown (voicestate.arm); the page asks /voice/commit at zero, and any
stop word, Esc, tap or Chat mode stops it. Spoken lines are built here from the server's
own copy of the card, and claim sent / saved / done only with a verified receipt.
"""

from __future__ import annotations

import email.utils
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from assistant import agent, autonomy, flow, manage, memory, obs, voice, voicestate

DEFAULT_CONF_MIN = 0.60
BODY_MAX = 300  # characters of an email body read aloud
QUOTE_MAX = 160  # characters of the body on the focus card

FINE_SAVE = "Saves to your Gmail drafts · only a fresh word from the mic counts · expires in 30 s"
FINE_TIER1 = "Only a fresh word from the mic counts · expires in 30 s"
LABEL_APPROVAL = "AWAITING VOICE APPROVAL"
LABEL_LEAVES = "AWAITING VOICE APPROVAL · LEAVES YOUR ACCOUNT"
LABEL_COUNT = "SENDING · SAY “STOP” TO CANCEL"
LABEL_DONE = "DONE · VERIFIED BY GMAIL"
LABEL_STOPPED = "STOPPED · NOTHING SENT"

SAVED_LINE = "Saved to your drafts, and Gmail confirmed it. Want me to send it now? Say send it."
NOTHING_WAITING = "There is nothing waiting for your approval right now."
SCREEN_ONLY = "That one is screen-only. Switch to Chat and use Settings."

# One fixed spoken reason per failed check. Nothing is written when any of them is said.
REASONS = {
    "mode": "Voice approvals only work in Voice mode. Nothing was changed.",
    "stale": "I didn't get that fresh from the mic, so it doesn't count. Please say it again.",
    "overlap": "I heard that while I was speaking, so it doesn't count. Say it again after I finish.",
    "low_conf": "I'm not sure I heard that right, so nothing was done. Please say it again.",
    "not_exact": "To approve, say just the words on the card, on their own. Nothing was done.",
    "no_card": NOTHING_WAITING,
    "screen_only": SCREEN_ONLY,
}

_RSVP_WORD = {"accepted": "accept it", "declined": "decline it", "tentative": "say maybe"}
_STOP_RE = re.compile(r"\b(?:stop|wait|cancel|no|nope|hold|don'?t|do not|not now|abort|halt|never ?mind)\b", re.I)


def conf_min() -> float:
    """VAANI_VOICE_CONF_MIN (default 0.60), read at call time."""
    try:
        v = float(os.environ.get("VAANI_VOICE_CONF_MIN", "").strip() or DEFAULT_CONF_MIN)
    except ValueError:
        v = DEFAULT_CONF_MIN
    return max(0.0, min(1.0, v))


# --------------------------------------------------------------------------- tiers


def _ap(word: str, tier: int, variant: str = "") -> dict:
    return {"word": word, "tier": tier, "variant": variant}


def _own() -> str:
    try:
        from assistant import tools

        return str(tools._own_address() or "").strip().lower()
    except Exception:
        return ""


def _guests(draft: dict) -> list[str]:
    """Other people on the event. The user's own address is not a guest: an event that only lists
    the user never leaves the account (the model sometimes adds the owner as an attendee)."""
    own = _own()
    out = []
    for a in draft.get("attendees") or []:
        addr = email.utils.parseaddr(str(a))[1].strip().lower() or str(a).strip().lower()
        if addr and addr != own:
            out.append(str(a))
    return out


def approvals(draft: dict | None) -> list[dict]:
    """The words that approve this card, each with its tier and commit_pending variant
    (contract table). The first one is the card's main word."""
    d = draft or {}
    kind = d.get("kind") or "event_create"
    if kind == "email_send":
        if d.get("gmail_draft_id"):
            return [_ap("send it", 2)]
        return [_ap("confirm", 1, "save_draft"), _ap("send it", 2)]
    if kind == "email_modify":
        if d.get("action") == "trash":
            return [_ap("trash it", 2), _ap("delete it", 2)]
        return [_ap("confirm", 1)]
    if kind == "event_rsvp":
        w = _RSVP_WORD.get(str(d.get("response") or ""))
        return [_ap(w, 2)] if w else []
    guests = bool(_guests(d))
    if kind == "event_create":
        return [_ap("invite them", 2)] if guests else [_ap("confirm", 1)]
    if kind == "event_update":
        return [_ap("move it", 2)] if guests else [_ap("confirm", 1)]
    if kind == "event_delete":
        return [_ap("delete it", 2)] if guests else [_ap("confirm", 1)]
    return []


def say_words(draft: dict | None) -> list[str]:
    """What the focus card tells you to say."""
    d = draft or {}
    kind = d.get("kind") or "event_create"
    if kind == "email_send":
        return ["send it", "not now"] if d.get("gmail_draft_id") else ["confirm", "send it", "change it", "cancel"]
    words = [a["word"] for a in approvals(d)][:1]
    if kind in ("event_create", "event_update"):
        words.append("change it")
    return words + ["cancel"]


def tier_of(draft: dict | None) -> dict:
    """{"tier": 1 | 2, "word": the main word, "words": say_words, "variant": the main word's variant}."""
    aps = approvals(draft)
    main = aps[0] if aps else _ap("", 2)
    return {"tier": main["tier"], "word": main["word"], "words": say_words(draft), "variant": main["variant"]}


def word_of(cmd) -> str:
    """The canonical approval word of a parsed command ("save it" is the confirm family)."""
    if getattr(cmd, "intent", "") == "confirm":
        return "confirm"
    return str(getattr(cmd, "word", "") or "").strip().lower()


# --------------------------------------------------------------------------- read-back text


def _first(name: str) -> str:
    return (name or "").split()[0] if (name or "").split() else ""


def _pretty_addr(addr: str) -> str:
    """sam.lee@example.org -> Sam Lee; a relay address (reply+abc123@...) -> ""."""
    local = str(addr or "").split("@", 1)[0]
    parts = [p for p in re.split(r"[._-]+", local) if p]
    if not parts or not all(p.isalpha() for p in parts) or len(parts) > 3:
        return ""
    return " ".join(p.capitalize() for p in parts)


def _person(draft: dict, first: bool = True) -> str:
    """The recipient as Vaani says it: "Ravi", or "Sam Lee"-style from the address, never the address."""
    name = str(draft.get("to_name") or "").strip()
    if not name:
        to = draft.get("to") or draft.get("attendees") or []
        name = _contact_name(to[0]) if to else ""
    name = voice.speakable(name).rstrip(".")
    if not name:
        return "them"
    return _first(name) if first else name


def _contact_name(addr: str) -> str:
    try:
        contacts = memory.load().get("contacts") or []
    except Exception:
        contacts = []
    want = str(addr or "").lower()
    for c in contacts:
        if isinstance(c, dict) and str(c.get("email") or "").lower() == want and c.get("name"):
            return str(c["name"])
    return _pretty_addr(addr)


def _names(addrs: list[str]) -> str:
    names = [_contact_name(a) or "a guest" for a in addrs]
    if len(names) <= 2:
        return " and ".join(names)
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"


_GREETING = re.compile(r"^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b[^\n]{0,40}[,!]?\s*$", re.I)
_SIGNOFF = re.compile(r"^(?:best|thanks|thank you|regards|kind regards|best regards|cheers|warmly|sincerely|thx)\b[\s\S]{0,20}$", re.I)


def _body_words(body: str, limit: int = BODY_MAX) -> str:
    """The email body as Vaani reads it: no greeting line, no sign-off, speakable, capped."""
    lines = [ln.strip() for ln in str(body or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if lines and _GREETING.match(lines[0]):
        lines = lines[1:]
    try:
        me = str(memory.load().get("name") or "").strip().lower()
    except Exception:
        me = ""
    while lines and (_SIGNOFF.match(lines[-1]) or (me and lines[-1].strip(" ,.").lower() in (me, me.split()[0]))):
        lines = lines[:-1]
    cut = lines.index("---------- Forwarded message ----------") if "---------- Forwarded message ----------" in lines else None
    if cut is not None:
        lines = lines[:cut]
    text = voice.speakable("\n".join(lines))
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def _spoken_when(iso: str | None) -> str:
    """2026-10-15T10:00:00+05:30 -> "Thursday 15 October at 10:00"; a date -> "Thursday 15 October"."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return ""
    if "T" not in str(iso):
        return f"{dt:%A} {dt.day} {dt:%B}"
    try:
        dt = dt.astimezone(agent._tz())
    except Exception:
        pass
    return f"{dt:%A} {dt.day} {dt:%B} at {dt:%H:%M}"


def _plural(n: int, word: str = "email") -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


_MODIFY_SAY = {
    "archive": "Archive {n}", "unarchive": "Move {n} back to the inbox", "mark_read": "Mark {n} as read",
    "mark_unread": "Mark {n} as unread", "star": "Star {n}", "unstar": "Unstar {n}", "trash": "Move {n} to Trash",
    "untrash": "Restore {n} from Trash", "label": "Add the label {label} to {n}", "unlabel": "Remove the label {label} from {n}",
}


def _first_sender(draft: dict) -> str:
    lines = ((draft.get("card") or {}).get("lines") or [])
    if not lines:
        return ""
    return voice.speakable(str(lines[0]).split(" - ", 1)[0]).rstrip(".")


def _how_to(draft: dict) -> str:
    """The closing sentence: which word to say and what happens."""
    t = tier_of(draft)
    if t["tier"] == 2:
        return f"Say {t['word']}, then I'll count down from five."
    return "Say confirm to go ahead."


def readback_text(draft: dict | None) -> str:
    """What Vaani says to read a card back, built in code from the server's own copy of the card
    (also used by /voice/tts for readback_draft_id). No URLs, ids or email addresses."""
    d = draft or {}
    kind = d.get("kind") or "event_create"
    if kind == "email_send":
        if d.get("gmail_draft_id"):
            return SAVED_LINE
        what = {"reply": "the reply", "forward": "the forward", "nudge": "the follow-up"}.get(str(d.get("mode") or ""), "the email")
        body = _body_words(d.get("body") or "")
        lead = f"Here's {what} to {_person(d)}"
        return f"{lead}: {body} Say confirm to save it to your drafts, or send it to send it." if body else \
            f"{lead}. Say confirm to save it to your drafts, or send it to send it."
    if kind == "email_modify":
        act = str(d.get("action") or "")
        n = len(d.get("ids") or [])
        label = voice.speakable(str(d.get("label_name") or "")).rstrip(".")
        verb = _MODIFY_SAY.get(act, "Change {n}").format(n=_plural(n), label=label)
        who = _first_sender(d)
        head = f"{verb}, starting with {who}." if who and n > 1 else (f"{verb}, from {who}." if who else f"{verb}.")
        if act == "trash":
            return f"{head} Say trash it, then I'll count down from five."
        return f"{head} {_how_to(d)}"
    guests = _guests(d)
    with_whom = f"with {_names(guests)}" if guests else "no guests"
    summary = voice.speakable(str(d.get("summary") or "the event")).rstrip(".")
    if kind == "event_create":
        when = _spoken_when(d.get("start"))
        return f"New event: {summary}, {when}, {with_whom}. {_how_to(d)}".replace(", ,", ",")
    if kind == "event_update":
        old = voice.speakable(str(d.get("old_summary") or summary)).rstrip(".")
        patch = d.get("patch") or {}
        parts = []
        if "start" in patch:
            parts.append(f"Move {old} to {_spoken_when((patch.get('start') or {}).get('dateTime'))}")
        if "summary" in patch:
            parts.append(f"rename it to {summary}" if parts else f"Rename {old} to {summary}")
        head = ", and ".join(parts) or f"Change {old}"
        return f"{head}, {with_whom}. {_how_to(d)}"
    when = _spoken_when(d.get("when_iso"))
    when_part = f", {when}" if when else ""
    if kind == "event_delete":
        return f"Delete {summary}{when_part}, {with_whom}. {_how_to(d)}"
    if kind == "event_rsvp":
        verb = {"accepted": "Accept", "declined": "Decline", "tentative": "Answer maybe to"}.get(str(d.get("response")), "Answer")
        return f"{verb} the invite to {summary}{when_part}. {_how_to(d)}"
    return voice.speakable(str((d.get("card") or {}).get("title") or ""))


def countdown_line(draft: dict) -> str:
    """Said when a tier-2 card is armed. Never claims anything happened."""
    kind = draft.get("kind") or "event_create"
    if kind == "email_send":
        return f"Sending to {_person(draft)} in five seconds. Say stop to cancel."
    if kind == "email_modify":
        return f"Moving {_plural(len(draft.get('ids') or []))} to Trash in five seconds. Say stop to cancel."
    if kind == "event_rsvp":
        return f"Answering the invite in five seconds. Say stop to cancel."
    verb = {"event_create": "Adding the event", "event_update": "Changing the event", "event_delete": "Deleting the event"}.get(kind, "Going ahead")
    return f"{verb} in five seconds. Say stop to cancel."


def stopped_line(draft: dict | None) -> str:
    d = draft or {}
    if (d.get("kind") or "") == "email_send":
        return "Stopped. Nothing was sent, and the draft is still saved." if d.get("gmail_draft_id") else "Stopped. Nothing was sent."
    return "Stopped. Nothing was changed."


# --------------------------------------------------------------------------- focus cards


def _quote(draft: dict) -> str | None:
    body = _body_words(draft.get("body") or "", QUOTE_MAX)
    return f"“{body}”" if body else None


def _title(draft: dict) -> str:
    kind = draft.get("kind") or "event_create"
    if kind == "email_send":
        what = {"reply": "Reply", "forward": "Forward", "nudge": "Follow-up"}.get(str(draft.get("mode") or ""), "Email")
        who = _person(draft, first=False)
        if draft.get("gmail_draft_id"):
            return f"Send the {what.lower()} to {who}"
        return f"{what} to {who}"
    return voice.speakable(str((draft.get("card") or {}).get("title") or "")).rstrip(".") or "Card"


def _sub(draft: dict) -> str:
    kind = draft.get("kind") or "event_create"
    if kind == "email_send":
        return voice.speakable(str(draft.get("subject") or "")).rstrip(".")
    if kind == "email_modify":
        who = _first_sender(draft)
        return f"First: {who}" if who else ""
    when = _spoken_when(draft.get("start") if kind == "event_create" else draft.get("when_iso"))
    guests = _guests(draft)
    return ", ".join(x for x in (when, f"with {_names(guests)}" if guests else "no guests") if x)


def focus_approval(draft: dict) -> dict:
    t = tier_of(draft)
    kind = draft.get("kind") or "event_create"
    if kind == "email_send" and draft.get("gmail_draft_id"):
        label, fine = LABEL_LEAVES, "Sending needs the words “send it”, then a 5-second countdown you can stop"
    elif kind == "email_send":
        label, fine = LABEL_APPROVAL, FINE_SAVE
    elif t["tier"] == 2:
        label, fine = LABEL_LEAVES, f"Needs the words “{t['word']}”, then a 5-second countdown you can stop"
    else:
        label, fine = LABEL_APPROVAL, FINE_TIER1
    return {"kind": "approval", "label": label, "title": _title(draft), "sub": _sub(draft),
            "quote": _quote(draft) if kind == "email_send" else None, "say": t["words"], "fine": fine, "tone": "amber"}


def focus_countdown(draft: dict) -> dict:
    kind = draft.get("kind") or "event_create"
    title = f"Sending to {_person(draft, first=False)}" if kind == "email_send" else _title(draft)
    label = LABEL_COUNT if kind == "email_send" else "COUNTDOWN · SAY “STOP” TO CANCEL"
    return {"kind": "count", "label": label, "title": title, "sub": _sub(draft),
            "quote": _quote(draft) if kind == "email_send" else None, "say": ["stop"], "fine": "", "tone": "red"}


def focus_stopped(draft: dict | None) -> dict:
    d = draft or {}
    if (d.get("kind") or "") == "email_send":
        sub = "The draft is still in Gmail" if d.get("gmail_draft_id") else "Nothing left your account"
        return {"kind": "receipt", "label": LABEL_STOPPED, "title": "Not sent", "sub": sub, "quote": None, "say": [],
                "fine": "", "tone": "amber"}
    return {"kind": "receipt", "label": "STOPPED · NOTHING CHANGED", "title": "Not done", "sub": "Nothing was changed",
            "quote": None, "say": [], "fine": "", "tone": "amber"}


def focus_receipt(draft: dict, rec: dict | None) -> dict:
    ok = voice.receipt_ok(rec)
    kind = draft.get("kind") or "event_create"
    status = str((rec or {}).get("status") or "")
    if not ok:
        return {"kind": "receipt", "label": "NOT VERIFIED · CHECK GOOGLE", "title": "Not confirmed",
                "sub": "Google did not confirm it", "quote": None, "say": [], "fine": "", "tone": "amber"}
    if kind == "email_send" and status == "SAVED_DRAFT":
        title, sub = "Saved to your Gmail drafts", f"{_title(draft)} · read back from Gmail"
    elif kind == "email_send":
        title, sub = f"Sent to {_person(draft, first=False)}", "Found in your Sent folder"
    else:
        report = (rec or {}).get("report") or []
        title = voice.speakable(str(report[0])).rstrip(".") if report else "Done"
        sub = "Read back from Gmail" if kind == "email_modify" else "Read back from Google Calendar"
    label = "SENT · VERIFIED" if status == "SENT" else (LABEL_DONE if kind.startswith("email") else "DONE · VERIFIED BY GOOGLE")
    return {"kind": "receipt", "label": label, "title": title, "sub": sub, "quote": None, "say": [], "fine": "", "tone": "green"}


# --------------------------------------------------------------------------- the voice block


def block(intent: str, transcript=None, **kw) -> dict:
    """The contract's "voice" block. Missing fields default to None ("phase" to listening)."""
    out = {"intent": intent, "heard": getattr(transcript, "text", "") or "",
           "confidence": round(transcript.confidence, 2) if transcript is not None else None,
           "spoken": "", "readback": None, "countdown": None, "approved": None, "focus": None,
           "phase": "listening", "client": None}
    out.update(kw)
    out["spoken"] = voice.speakable(out["spoken"] or "") if out["spoken"] else ""
    return out


def readback_block(draft: dict) -> dict:
    t = tier_of(draft)
    return {"draft_id": draft["draft_id"], "tier": t["tier"], "words": t["words"]}


def with_card(vb: dict, session) -> dict:
    """A result that leaves a NEW card pending in voice mode: read it back, show it, wait for a word.
    A card that was already read back earlier stays waiting quietly: "next" or "read it" still walks
    the inbox, with one short reminder; "repeat" (or a stale read-back at approval time) reads it again."""
    draft = agent.pending_draft(session)
    if not draft or vb.get("countdown"):
        return vb
    if voicestate.last_readback(getattr(session, "id", ""), draft["draft_id"]) is not None:
        spoken = str(vb.get("spoken") or "").strip()
        vb["spoken"] = f"{spoken} {STILL_WAITING}".strip()
        return vb
    vb["readback"] = readback_block(draft)
    vb["spoken"] = readback_text(draft)
    vb["focus"] = focus_approval(draft)
    vb["phase"] = "approval"
    return vb


STILL_WAITING = "Your earlier card is still waiting; say repeat to hear it."


def code_out(session, heard: str | None, reply: str, t0: float, append: bool = True) -> dict:
    """A /chat-shaped dict for a reply built in code. The heard line and the reply go into the
    transcript, so the model's next turn sees the same conversation as the page."""
    if append:
        session.turn += 1
        if heard:
            session.messages.append({"role": "user", "content": f"{agent._now_tag()} {heard}"})
        session.messages.append({"role": "assistant", "content": reply})
    return flow._out(agent._result(session, reply, [], agent._zero_usage(), t0))


# --------------------------------------------------------------------------- the seven checks


@dataclass
class Verdict:
    ok: bool
    code: str = ""  # "" | mode | stale | overlap | low_conf | not_exact | no_card | wrong_word | readback
    spoken: str = ""
    draft: dict | None = None
    approval: dict | None = None
    extra: dict = field(default_factory=dict)


def check_fresh(session_id: str, transcript, cmd=None, exact: bool = True) -> Verdict | None:
    """Checks 1-5 (voice mode, fresh transcript, no overlap, confidence, exact). None = all pass."""
    if not voicestate.is_voice(session_id):
        return Verdict(False, "mode", REASONS["mode"])
    if transcript is None or str(getattr(transcript, "session_id", "")) != str(session_id):
        return Verdict(False, "stale", REASONS["stale"])
    if transcript.overlap:
        return Verdict(False, "overlap", REASONS["overlap"])
    if transcript.confidence < conf_min():
        return Verdict(False, "low_conf", REASONS["low_conf"])
    if exact and not getattr(cmd, "exact", False):
        return Verdict(False, "not_exact", REASONS["not_exact"])
    return None


def wrong_word_line(draft: dict) -> str:
    t = tier_of(draft)
    words = [a["word"] for a in approvals(draft)]
    if (draft.get("kind") or "") == "email_send" and not draft.get("gmail_draft_id"):
        return "For this one, say confirm to save it to your drafts, or send it to send it."
    if t["tier"] == 2:
        return f"That one needs the words {words[0]}. Say {words[0]}, then I'll count down from five."
    return "For this one, say confirm to go ahead."


def check(session, session_id: str, transcript, cmd) -> Verdict:
    """All seven checks for a spoken approval of the session's pending card. The transcript
    must come from voicestate.take_transcript (the route takes it; None = not fresh)."""
    bad = check_fresh(session_id, transcript, cmd)
    if bad is not None:
        bad.draft = agent.pending_draft(session)
        return bad
    draft = agent.pending_draft(session)
    if draft is None:
        return Verdict(False, "no_card", REASONS["no_card"])
    word = word_of(cmd)
    approval = next((a for a in approvals(draft) if a["word"] == word), None)
    if approval is None:
        return Verdict(False, "wrong_word", wrong_word_line(draft), draft=draft)
    did = str(draft["draft_id"])
    rb = voicestate.last_readback(session_id, did)
    st = voicestate.peek(session_id)
    fresh_rb = (
        rb is not None
        and rb.complete is True
        and rb.ended_at is not None
        and session.pending_draft_id == did
        and (session.drafts.get(did) or {}).get("status") == "DRAFT"
        and transcript.capture_start >= rb.ended_at - voicestate.SPEECH_MARGIN_S
        and transcript.capture_start <= rb.ended_at + voicestate.READBACK_TTL_S
        # a stop after the read-back (a stopped countdown, a cut-off) needs a fresh read-back
        and not (st is not None and st.last_stop_at is not None and st.last_stop_at >= rb.ended_at)
    )
    if not fresh_rb:
        return Verdict(False, "readback", readback_text(draft), draft=draft)
    return Verdict(True, "", "", draft=draft, approval=approval)


# --------------------------------------------------------------------------- commit and arm


def _voice_ctx(heard: str, confidence, tid: str):
    return obs.current_voice.set({"heard": heard, "confidence": confidence, "tid": tid})


def _done_line(draft: dict, rec: dict | None, variant: str) -> str:
    """What Vaani says after a commit: success words only with a verified receipt."""
    if not voice.receipt_ok(rec):
        return ""
    kind = draft.get("kind") or "event_create"
    summary = voice.speakable(str(draft.get("summary") or "the event")).rstrip(".")
    if kind == "email_send":
        if variant == "save_draft":
            return SAVED_LINE
        return f"Sent to {_person(draft)}, and Gmail confirmed it."
    if kind == "email_modify":
        line = voice.speakable(str(((rec or {}).get("report") or ["Done."])[0]))
        return f"{line} Say undo to put it back." if (rec or {}).get("undo") else line
    return {"event_create": f"Added {summary} to your calendar, and Google confirmed it.",
            "event_update": f"Changed {summary}, and Google confirmed it.",
            "event_delete": f"Deleted {summary}, and Google confirmed it.",
            "event_rsvp": f"Answered the invite to {summary}, and Google confirmed it."}.get(kind, "Done, and Google confirmed it.")


def after_commit(session, draft: dict, variant: str, out: dict, heard: str, confidence, tier: int, transcript=None) -> dict:
    """Shape the commit's /chat dict for voice: receipt focus, the spoken line, and after a
    verified "confirm" on an email, the saved-draft send card read back."""
    rec = out.get("receipt")
    ok = voice.receipt_ok(rec)
    approved = {"by": "voice", "heard": heard, "confidence": round(float(confidence), 2) if confidence is not None else None,
                "tier": tier}
    spoken = _done_line(draft, rec, variant)
    if not ok:
        # Never a success word here. The card may still be pending: show it, but do not read it
        # back over this line (a fresh read-back is needed before another try anyway).
        reply = str(out.get("reply") or "")
        sending = (draft.get("kind") or "") == "email_send" and variant != "save_draft"
        if "VAANI_BLOCK_SEND" in reply:
            spoken = "Sending is switched off on this server, so nothing was sent."
        elif rec is None:
            spoken = "I couldn't do that, so nothing was sent." if sending else "I couldn't do that. Nothing was changed."
        else:
            spoken = "Google did not confirm it. Please check before trying again."
        pending = agent.pending_draft(session)
        vb = block("confirm", transcript, spoken=spoken, approved=approved,
                   focus=focus_approval(pending) if pending else focus_receipt(draft, rec),
                   phase="approval" if pending else "listening")
        return {**out, "voice": vb}
    if (draft.get("kind") or "") == "email_send" and variant == "save_draft":
        s_tok = obs.current_session.set(session)
        try:
            card = manage.draft_send_saved(draft, session)
        finally:
            obs.current_session.reset(s_tok)
        if card is not None:
            out["pending_draft"] = agent.pending_draft(session)
            last = session.messages[-1] if session.messages else None
            if isinstance(last, dict) and last.get("role") == "assistant":
                last["content"] = f"{last.get('content') or ''}\nWant me to send it now? Say send it.".strip()
            out["reply"] = f"{out.get('reply') or ''}\nWant me to send it now? Say send it.".strip()
            vb = block("confirm", transcript, spoken=SAVED_LINE, approved=approved, readback=readback_block(card),
                       focus=focus_approval(card), phase="approval")
            return {**out, "voice": vb}
    vb = block("confirm", transcript, spoken=spoken, approved=approved, focus=focus_receipt(draft, rec), phase="done")
    return {**out, "voice": with_card(vb, session)}


def commit_now(session, session_id: str, verdict: Verdict, transcript) -> dict:
    """Tier 1: carry the card out at once, through the click's own path, confirmed_by "voice"."""
    draft, ap = verdict.draft, verdict.approval
    v_tok = _voice_ctx(transcript.text, transcript.confidence, transcript.tid)
    try:
        out = flow.confirm(session, draft["draft_id"], ap["variant"] or None, confirmer="voice")
    finally:
        obs.current_voice.reset(v_tok)
    return after_commit(session, session.drafts.get(draft["draft_id"]) or draft, ap["variant"], out, transcript.text,
                        transcript.confidence, ap["tier"], transcript)


def arm_countdown(session, session_id: str, verdict: Verdict, transcript, t0: float) -> dict:
    """Tier 2: start the 5-second countdown. Nothing is written until /voice/commit at zero."""
    draft, ap = verdict.draft, verdict.approval
    a = voicestate.arm(session_id, draft["draft_id"], ap["tier"], ap["word"], transcript, variant=ap["variant"])
    line = countdown_line(draft)
    out = code_out(session, transcript.text, line, t0)
    vb = block("action", transcript, spoken=line, focus=focus_countdown(draft), phase="countdown",
               countdown={"arm_id": a.arm_id, "draft_id": draft["draft_id"], "seconds": int(voicestate.COUNTDOWN_S),
                          "word": ap["word"]})
    return {**out, "voice": vb}


def commit_armed(session, session_id: str, a) -> dict:
    """/voice/commit after every condition held and voicestate.mark_committed claimed the arm."""
    draft = session.drafts.get(a.draft_id) or {}
    v_tok = _voice_ctx(a.heard, a.confidence, a.tid)
    try:
        out = flow.confirm(session, a.draft_id, a.variant or None, confirmer="voice")
    finally:
        obs.current_voice.reset(v_tok)

    class _T:  # the heard line of the approval that armed it
        text, confidence = a.heard, a.confidence

    return after_commit(session, draft, a.variant, out, a.heard, a.confidence, a.tier, _T)


def stopped_result(session, draft: dict | None, heard: str | None, t0: float, intent: str = "stop", transcript=None) -> dict:
    line = stopped_line(draft)
    out = code_out(session, heard, line, t0)
    vb = block(intent, transcript, spoken=line, focus=focus_stopped(draft), phase="listening", client="stop_speaking")
    return {**out, "voice": vb}


def is_stop_word(cmd, text: str) -> bool:
    """During a countdown every stop word counts: stop, wait, cancel, no, hold on, don't."""
    return getattr(cmd, "intent", "") in ("stop", "cancel") or bool(_STOP_RE.search(str(text or "")))


def undo_target(session) -> str | None:
    """The newest carried-out card whose receipt offers Undo."""
    for did in reversed(list(session.drafts)):
        if autonomy.undo_plan(session.drafts[did]) is not None:
            return did
    return None


def now() -> float:
    return time.perf_counter()
