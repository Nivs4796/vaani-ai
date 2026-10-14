"""Code-built fast paths for voice mode (PARSER): brief, today's calendar, next meeting,
next / read email, all answered without the model in about 1-2 s.

handle() returns the /chat-shaped dict (flow._out(agent._result(...))) plus a "voice"
block, and keeps session.messages exactly as a typed turn would: the user line, any
tool call with its result (run through agent._seed_tool_call, so the @logged tool shows
as a live step and the model's next turn can use it), then the reply. The email in
focus is always READ (read_email), so agent._email_in_focus finds it and "reply saying
..." answers that email next. The walk through the needs-reply emails lives in
VoiceState.data["fp.walk"] = {"ids", "i", "rows"}.

Spoken text is built here, run through voice.speakable (no markdown, URLs, ids or email
addresses) and never claims anything was sent, saved or created. Contract:
.build/CONTRACT-v3.md.
"""

from __future__ import annotations

import email.utils
import json
import math
import re
import time
from datetime import datetime

from assistant import agent, brief, flow, obs, voice

WALK = "fp.walk"
SPOKEN_MAX = 320  # characters of one spoken read-out
QUOTE_MAX = 280  # characters of the email shown on the focus card
SOON_MIN = 120  # the greeting mentions the next meeting when it starts within this many minutes
_NUM = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
_COMMON_FIRST = {"design", "weekly", "team", "call", "meeting", "sync", "review", "standup", "stand-up", "daily", "lunch",
                 "dinner", "demo", "interview", "catch-up", "catch", "check-in", "planning", "sprint", "project", "product",
                 "client", "quick", "monthly", "board", "kickoff", "kick-off", "retro", "retrospective", "coffee", "chat",
                 "intro", "follow-up", "focus", "office", "budget", "launch", "status", "all-hands", "town", "workshop",
                 "training", "onboarding", "doctor", "gym", "flight", "school", "pickup", "dentist"}


# --------------------------------------------------------------------------- small helpers

def _num(n: int) -> str:
    return _NUM[n] if 0 <= n < len(_NUM) else str(n)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _say(text: str) -> str:
    return voice.speakable(text)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def _person(sender: str, from_header: str = "") -> str:
    """'Ravi Mehta (Basecamp)' -> 'Ravi Mehta'; 'sam.lee@example.org' -> 'Sam Lee'; never an address."""
    s = " ".join(re.sub(r"\(.*?\)", "", str(sender or "")).split()).strip(' "\'')
    if not s or "@" in s:
        disp, addr = email.utils.parseaddr(str(from_header or sender or ""))
        disp = " ".join(re.sub(r"\(.*?\)", "", disp or "").split()).strip(' "\'')
        if disp and "@" not in disp:
            return disp
        local = (addr or s).split("@")[0]
        words = [w for w in re.split(r"[._\-+]+", local) if w and not w.isdigit()]
        return " ".join(w.capitalize() for w in words) or "Someone"
    return s


def _end(text: str) -> str:
    """A full stop unless the text already ends a sentence ("... on Thursday?" stays a question)."""
    text = str(text or "").rstrip()
    return text if text[-1:] in (".", "!", "?", "…") else text + "."


def _first(full_name: str) -> str:
    return full_name.split()[0] if full_name.split() else full_name


def _subject(subject: str) -> str:
    s = " ".join(str(subject or "").split())
    s = re.sub(r"^(?:(?:re|fwd?|aw|sv)\s*:\s*)+", "", s, flags=re.I)
    return s or "no subject"


def _day_word(date_text: str) -> str:
    """'2026-10-13 17:42' -> 'Today' / 'Yesterday' / '12 Oct' (user's timezone)."""
    try:
        d = datetime.strptime(str(date_text)[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return str(date_text or "")
    today = datetime.now(agent._tz()).date()
    delta = (today - d.date()).days
    if delta == 0:
        return "Today " + d.strftime("%H:%M")
    if delta == 1:
        return "Yesterday"
    return f"{d.day} {d:%b}"


def _clock(dt: datetime) -> str:
    """Spoken time: 11 am, 2:30 pm, noon."""
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    h = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h} {ampm}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {ampm}"


def _mins_until(start: datetime, now: datetime) -> int:
    """Whole minutes until start, rounded up: 17 min 20 s left is "in 18 minutes"."""
    return math.ceil((start - now).total_seconds() / 60)


def _in_words(minutes: int) -> str:
    if minutes <= 1:
        return "in a minute" if minutes == 1 else "now"
    if minutes < 60:
        return f"in {minutes} minutes"
    h, m = divmod(minutes, 60)
    hours = "an hour" if h == 1 else f"{h} hours"
    return f"in {hours}" + (f" and {m} minutes" if m else "")


def _in_short(minutes: int) -> str:
    if minutes < 60:
        return f"in {max(0, minutes)} min"
    h, m = divmod(minutes, 60)
    return f"in {h} h {m} min" if m else f"in {h} h"


def _title(summary: str) -> str:
    t = " ".join(str(summary or "").split()) or "an untitled event"
    return _say(t).rstrip(".") or "an untitled event"


def _your(title: str) -> str:
    """'Design review' -> 'your design review'; 'Ravi / Alex sync' stays as it is."""
    first = title.split()[0] if title.split() else ""
    if first.lower() in _COMMON_FIRST and first[:1].isupper() and first[1:].islower():
        return "your " + title[:1].lower() + title[1:]
    return title


def _voice(cmd, spoken: str, focus: dict | None, phase: str = "listening") -> dict:
    return {"intent": cmd.intent, "heard": cmd.text, "confidence": None, "spoken": spoken, "readback": None,
            "countdown": None, "approved": None, "focus": focus, "phase": phase, "client": None}


def _focus(kind: str, label: str, title: str, sub: str = "", quote: str = "") -> dict:
    return {"kind": kind, "label": label, "title": title, "sub": sub, "quote": quote, "say": [], "fine": "", "tone": "acc"}


# --------------------------------------------------------------------------- the turn wrapper

class _Turn:
    """One fast-path turn: the user line in, tool calls seeded, the reply out, the dict built."""

    def __init__(self, session, cmd):
        self.session, self.cmd = session, cmd
        self.t0 = time.perf_counter()
        self.loop = agent._Loop(session)
        self.pre_len = len(session.messages)
        words = cmd.text or cmd.raw or cmd.intent
        session.turn += 1
        session.messages.append({"role": "user", "content": f"{agent._now_tag(words, session)} {words}"})

    def tool(self, name: str, args: dict) -> tuple[dict | None, dict]:
        result, record = agent._seed_tool_call(self.session, self.loop, name, args)
        if not record["ok"]:
            return None, record
        try:
            return json.loads(result), record
        except ValueError:
            return None, record

    def done(self, reply: str, spoken: str, focus: dict | None) -> dict:
        self.session.messages.append({"role": "assistant", "content": reply})
        res = agent._result(self.session, reply, self.loop.tool_records, agent._zero_usage(), self.t0)
        res.footer = f"{res.footer} · voice fast path, no model call"
        out = flow._out(res)
        out["voice"] = _voice(self.cmd, spoken, focus)
        return out

    def rollback(self) -> None:
        del self.session.messages[self.pre_len:]


# --------------------------------------------------------------------------- email walk

def _needs_rows(b: dict) -> tuple[list[dict], int]:
    for g in (b or {}).get("groups") or []:
        if g.get("key") == "needs_reply":
            return [r for r in g.get("rows") or [] if r.get("id")], int(g.get("count") or 0)
    return [], 0


def _listing_in_transcript(session) -> bool:
    return any(m.get("role") == "tool" and m.get("tool_name") == "list_recent_emails" for m in session.messages)


def _email_card(row: dict, i: int, n: int, quote: str = "") -> dict:
    label = f"NOW · EMAIL {i + 1} OF {n}" if n else "NOW · EMAIL"
    sub = " · ".join(x for x in (_subject(row.get("subject")), _day_word(row.get("date"))) if x)
    return _focus("email", label, _person(row.get("sender"), row.get("from")), sub,
                  _clip(quote or row.get("snippet") or "", QUOTE_MAX))


def _walk_row(vstate) -> tuple[dict | None, int, int]:
    walk = (getattr(vstate, "data", None) or {}).get(WALK) or {}
    rows, i = walk.get("rows") or [], int(walk.get("i") or 0)
    if 0 <= i < len(rows):
        return rows[i], i, len(rows)
    return None, i, len(rows)


def _brief(turn: _Turn, vstate) -> dict:
    session = turn.session
    b = getattr(session, "brief", None)
    fresh = False
    if not b or b.get("error") or not b.get("groups"):
        b, fresh = brief.build(), True  # cached per user; a fresh build reads Gmail through the @logged tool
        if not b.get("error"):
            session.brief = b
    if b.get("error"):
        spoken = "I couldn't read your inbox just now. Nothing was changed; try again in a moment."
        return turn.done(spoken, spoken, None)
    if (fresh or not _listing_in_transcript(session)) and b.get("tool_result"):
        # Same shape as flow.open_chat: "what does Ravi want?" finds the ids next.
        args = {"max_results": brief.MAX_RESULTS, "query": f"in:inbox newer_than:{b.get('covers_days') or 3}d"}
        session.messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_recent_emails", "arguments": args}}]})
        session.messages.append({"role": "tool", "content": b["tool_result"], "tool_name": "list_recent_emails"})
    rows, count = _needs_rows(b)
    keep = [{k: r.get(k) for k in ("id", "sender", "from", "subject", "date", "snippet")} for r in rows]
    vstate.data[WALK] = {"ids": [r["id"] for r in keep], "i": 0, "rows": keep}
    if not keep:
        unread = int(b.get("unread") or 0)
        spoken = "No emails need a reply right now." + (f" You have {unread} unread updates." if unread else "") \
            + " Want your calendar instead?"
        focus = _focus("email", f"INBOX · LAST {b.get('covers_days') or 3} DAYS", "Nothing needs a reply",
                       f"{unread} unread" if unread else "All caught up")
        return turn.done(_say(spoken), _say(spoken), focus)
    row = keep[0]
    data, _rec = turn.tool("read_email", {"message_id": row["id"]})  # puts the email in focus for "reply saying ..."
    who, subj = _person(row.get("sender"), row.get("from")), _subject(row.get("subject"))
    n = max(count, len(keep))
    if n == 1:
        spoken = f"One email needs a reply. {who}: {_end(subj)} Read it, or reply?"
    else:
        spoken = f"{_cap(_num(n))} emails need a reply. First, {who}: {_end(subj)} Read it, reply, or next?"
    spoken = _say(spoken)
    reply = f"{b.get('text') or ''}\n\n{spoken}".strip()
    return turn.done(reply, spoken, _email_card(row, 0, len(keep), row.get("snippet") or (data or {}).get("body") or ""))


def _next(turn: _Turn, vstate) -> dict:
    walk = vstate.data.get(WALK)
    if not walk or not walk.get("rows"):
        spoken = "Say “brief me” first and I will walk you through your inbox."
        return turn.done(spoken, spoken, None)
    rows = walk["rows"]
    i = int(walk.get("i") or 0) + 1
    walk["i"] = min(i, len(rows))
    if i >= len(rows):
        spoken = "That was the last one. Want your calendar instead?"
        return turn.done(spoken, spoken, None)
    row = rows[i]
    data, _rec = turn.tool("read_email", {"message_id": row["id"]})
    who, subj = _person(row.get("sender"), row.get("from")), _subject(row.get("subject"))
    lead = "Last one" if i == len(rows) - 1 else "Next"
    spoken = _say(f"{lead}, {who}: {_end(subj)}")
    return turn.done(spoken, spoken, _email_card(row, i, len(rows), row.get("snippet") or (data or {}).get("body") or ""))


_QUOTED_RE = re.compile(r"(?im)^\s*>.*$|^\s*on .{0,120}wrote:\s*$")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _body_sentences(body: str, n: int = 2) -> str:
    """The first n sentences of what the sender wrote: no quoted reply, no greeting line, speakable."""
    text = str(body or "")
    text = re.split(r"(?im)^\s*on .{0,160}wrote:\s*$|^-{2,}\s*original message|^\s*-- ?$", text)[0]
    text = _QUOTED_RE.sub("", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and re.match(r"(?i)^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b[^.!?]{0,40}[,!]?$", lines[0]):
        lines = lines[1:]
    text = _say(" ".join(lines))
    parts = [p for p in _SENTENCE_RE.split(text) if p.strip()]
    out = " ".join(parts[:n]).strip()
    return out


def _read(turn: _Turn, vstate) -> dict:
    row, i, n = _walk_row(vstate)
    mid = (row or {}).get("id")
    if not mid:
        focus = agent._email_in_focus(turn.session)  # an email read earlier in this chat
        mid = (focus or {}).get("id")
        row = {"id": mid, "from": (focus or {}).get("from"), "sender": "", "subject": (focus or {}).get("subject"),
               "date": (focus or {}).get("date")} if mid else None
        n = 0
    if not mid:
        spoken = "Which email? Say “brief me” and I will walk you through your inbox."
        return turn.done(spoken, spoken, None)
    data, rec = turn.tool("read_email", {"message_id": mid})
    if data is None:
        spoken = "I couldn't open that email just now. Nothing was changed; try again in a moment."
        return turn.done(spoken, spoken, None)
    who = _person(row.get("sender") or "", data.get("from") or row.get("from") or "")
    first = agent._recipient_first_name(data.get("from")) or who
    said = _body_sentences(data.get("body") or "")
    tail = " Reply, or next?" if n and i < n - 1 else " Want to reply?"
    room = SPOKEN_MAX - len(first) - len(" wrote: ") - len(tail)
    said = _clip(said, max(60, room)) if said else "nothing but the subject line, " + _subject(data.get("subject")) + "."
    if said and said[-1] not in ".!?…":
        said += "."
    spoken = _say(f"{first} wrote: {said}{tail}")
    row = {**row, "subject": data.get("subject") or row.get("subject"), "date": data.get("date") or row.get("date"),
           "from": data.get("from") or row.get("from")}
    return turn.done(spoken, spoken, _email_card(row, i if n else 0, n, data.get("body") or ""))


# --------------------------------------------------------------------------- calendar

def _events(data: dict | None) -> list[dict]:
    """Timed events with aware start/end datetimes (user tz), and all-day ones with start None."""
    tz = agent._tz()
    out = []
    for e in (data or {}).get("events") or []:
        if e.get("all_day"):
            out.append({**e, "_start": None, "_end": None})
            continue
        try:
            s = datetime.strptime(str(e.get("start"))[:16], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            raw_end = str(e.get("end") or "")
            end = datetime.strptime(raw_end[:16], "%Y-%m-%d %H:%M") if len(raw_end) >= 16 else \
                datetime.strptime(f"{s:%Y-%m-%d} {raw_end[:5]}", "%Y-%m-%d %H:%M")
            out.append({**e, "_start": s, "_end": end.replace(tzinfo=tz)})
        except ValueError:
            continue
    return out


def _meeting_sub(e: dict, now: datetime) -> str:
    bits = []
    if e.get("_start") is not None:
        mins = _mins_until(e["_start"], now)
        if mins >= 0:
            bits.append(_in_short(mins))
        elif e.get("_end") and e["_end"] > now:
            bits.append("on now · until " + e["_end"].strftime("%H:%M"))
    where = " ".join(str(e.get("location") or "").split())
    if where and "http" not in where and "@" not in where:
        bits.append(where if len(where) <= 40 else where[:39] + "…")
    people = len(e.get("attendees") or [])
    if people:
        bits.append(f"{people} {'person' if people == 1 else 'people'}")
    return " · ".join(b for b in bits if b)


def _calendar_today(turn: _Turn, vstate) -> dict:
    data, rec = turn.tool("list_calendar_events", {"on_date": "today"})
    if data is None:
        spoken = "I couldn't read your calendar just now. Nothing was changed; try again in a moment."
        return turn.done(spoken, spoken, None)
    now = datetime.now(agent._tz())
    evs = _events(data)
    timed = [e for e in evs if e["_start"] is not None]
    all_day = [e for e in evs if e["_start"] is None]
    later = [e for e in timed if e["_end"] > now]
    n = len(timed)
    parts = []
    if not evs:
        spoken = "Your calendar is clear today."
        focus = _focus("meeting", "TODAY", "Nothing on your calendar", now.strftime("%a %d %b"))
        return turn.done(spoken, spoken, focus)
    if n:
        head = f"You have {_num(n)} meeting{'s' if n != 1 else ''} today"
        if later and len(later) < n:
            head += f", {_num(len(later))} still to come"
        parts.append(head + ".")
    if later:
        e = later[0]
        if e["_start"] <= now:
            parts.append(f"{_cap(_title(e['summary']))} is on now, until {_clock(e['_end'])}.")
        else:
            mins = _mins_until(e["_start"], now)
            parts.append(f"Next is {_title(e['summary'])} at {_clock(e['_start'])}, {_in_words(mins)}.")
        rest = [f"{_title(x['summary'])} at {_clock(x['_start'])}" for x in later[1:4]]
        if rest:
            more = len(later) - 1 - len(rest)
            tail = ", ".join(rest[:-1]) + (" and " if len(rest) > 1 else "") + rest[-1]
            parts.append(f"Then {tail}" + (f", and {_num(more)} more." if more > 0 else "."))
    elif n:
        parts.append("They are all over for today.")
    if all_day:
        parts.append(("All day: " if n else "Today, all day: ") + ", ".join(_title(x["summary"]) for x in all_day[:3]) + ".")
    spoken = _say(" ".join(parts))
    if later:
        e = later[0]
        focus = _focus("meeting", f"TODAY · {n} MEETING{'S' if n != 1 else ''}",
                       f"{e['_start']:%H:%M} · {' '.join(str(e.get('summary') or '').split()) or '(no title)'}",
                       _meeting_sub(e, now),
                       " · ".join(f"{x['_start']:%H:%M} {' '.join(str(x.get('summary') or '').split())}" for x in later[1:4]))
    else:
        focus = _focus("meeting", "TODAY", "Nothing left today" if n else "All-day only",
                       f"{n} earlier" if n else ", ".join(str(x.get("summary") or "") for x in all_day[:3]))
    return turn.done(spoken, spoken, focus)


def _next_meeting(turn: _Turn, vstate) -> dict:
    data, rec = turn.tool("list_calendar_events", {"days_ahead": 7})
    if data is None:
        spoken = "I couldn't read your calendar just now. Nothing was changed; try again in a moment."
        return turn.done(spoken, spoken, None)
    now = datetime.now(agent._tz())
    timed = [e for e in _events(data) if e["_start"] is not None and e["_end"] > now]
    current = [e for e in timed if e["_start"] <= now]
    upcoming = [e for e in timed if e["_start"] > now]
    parts = []
    if current:
        parts.append(f"{_cap(_title(current[0]['summary']))} is on now, until {_clock(current[0]['_end'])}.")
    if not upcoming:
        parts.append("Nothing else on your calendar in the next seven days." if current
                     else "Nothing on your calendar in the next seven days.")
        spoken = _say(" ".join(parts))
        focus = _focus("meeting", "NEXT MEETING", "Nothing in the next 7 days", "") if not current else \
            _focus("meeting", "ON NOW", f"{current[0]['_start']:%H:%M} · {current[0].get('summary') or '(no title)'}",
                   _meeting_sub(current[0], now))
        return turn.done(spoken, spoken, focus)
    e = upcoming[0]
    days = (e["_start"].date() - now.date()).days
    mins = _mins_until(e["_start"], now)
    if days == 0:
        when = f"at {_clock(e['_start'])}, {_in_words(mins)}"
    elif days == 1:
        when = f"tomorrow at {_clock(e['_start'])}"
    else:
        when = f"on {e['_start']:%A} at {_clock(e['_start'])}"
    lead = "After that" if current else "Your next meeting is"
    parts.append(f"{lead}{',' if current else ''} {_title(e['summary'])} {when}.")
    spoken = _say(" ".join(parts))
    day_label = "" if days == 0 else ("Tomorrow · " if days == 1 else f"{e['_start']:%a %d %b} · ")
    focus = _focus("meeting", "NEXT MEETING", f"{day_label}{e['_start']:%H:%M} · {' '.join(str(e.get('summary') or '').split()) or '(no title)'}",
                   _meeting_sub(e, now))
    return turn.done(spoken, spoken, focus)


# --------------------------------------------------------------------------- public

_HANDLERS = {"brief": _brief, "next": _next, "read": _read, "calendar_today": _calendar_today, "next_meeting": _next_meeting}


def handle(session, cmd, vstate) -> dict | None:
    """A /chat-shaped dict (flow._out(...)) plus a "voice" block, or None when this is not a fast path."""
    fn = _HANDLERS.get(getattr(cmd, "intent", ""))
    if fn is None:
        return None
    if vstate is None or not isinstance(getattr(vstate, "data", None), dict):
        return None
    token = obs.current_session.set(session)
    turn = _Turn(session, cmd)
    try:
        return fn(turn, vstate)
    except Exception:
        turn.rollback()  # the transcript stays as it was; the caller falls back to the model
        return None
    finally:
        obs.current_session.reset(token)


def _greet_word(now: datetime) -> str:
    if 5 <= now.hour < 12:
        return "Good morning"
    if 12 <= now.hour < 17:
        return "Good afternoon"
    if 17 <= now.hour < 23:
        return "Good evening"
    return "Hello"


def _soon_meeting(now: datetime) -> tuple[str, int] | None:
    """(title, minutes) of the next timed meeting today starting within SOON_MIN minutes, else None."""
    from assistant import routes_rail

    data = routes_rail.today_meetings()
    best = None
    for e in data.get("events") or []:
        if e.get("all_day"):
            continue
        try:
            s = datetime.fromisoformat(str(e.get("start")))
        except ValueError:
            continue
        if s.tzinfo is None:
            s = s.replace(tzinfo=now.tzinfo)
        mins = _mins_until(s, now)
        if 0 < mins <= SOON_MIN and (best is None or mins < best[1]):
            best = (str(e.get("title") or ""), mins)
    return best


def greeting(session, name: str = "") -> str:
    """'Good afternoon, Alex. Three emails need a reply, and your design review is in 18 minutes.
    Where shall we start?' Built in code from the brief and today's calendar, user timezone."""
    try:
        now = datetime.now(agent._tz())
        first = _first(" ".join(str(name or "").split()))
        first = first if first and "@" not in first else ""
        line = f"{_greet_word(now)}, {first}." if first else f"{_greet_word(now)}."
        facts = []
        try:
            b = getattr(session, "brief", None)
            if not b or b.get("error") or not b.get("groups"):
                b = brief.build()
            if b and not b.get("error"):
                rows, count = _needs_rows(b)
                n = max(count, len(rows))
                facts.append("No emails need a reply" if n == 0 else
                             "One email needs a reply" if n == 1 else f"{_cap(_num(n))} emails need a reply")
        except Exception:
            pass
        try:
            soon = _soon_meeting(now)
            if soon:
                title = _title(soon[0])
                facts.append(f"{_your(title)} {'starts now' if soon[1] <= 1 else 'is ' + _in_words(soon[1])}")
        except Exception:
            pass
        if facts:
            line += " " + _cap(facts[0]) + (f", and {facts[1]}" if len(facts) > 1 else "") + "."
        return _say(line + " Where shall we start?")
    except Exception:
        return "Hello. Where shall we start?"
