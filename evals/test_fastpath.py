"""Offline checks for the voice-mode fast paths (assistant/fastpath.py), the greeting and
the shorter voice-mode model context (assistant/voicectx.py).

Fake Gmail and Calendar (evals/test_manage.py fakes), temp memory, a stand-in model for
the one follow-up that needs it. Checked: each fast path's spoken text and focus card,
the "voice" block, the transcript left behind (well-formed, the email in focus found by
agent._email_in_focus, so "reply saying thanks" answers it), the spoken-text rules, code
time with fakes, the trim invariants on long transcripts, and the greeting.

Run: .venv/bin/python -m evals.test_fastpath   (exit 1 on any failure)
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from evals import test_manage as tm

from assistant import agent, brief, fastpath, flow, google_auth, obs, tools, voicecmd, voicectx, voicestate

CHECKS: list[tuple[str, bool, str]] = []
TIMES: list[tuple[str, float]] = []
VOICE_KEYS = {"intent", "heard", "confidence", "spoken", "readback", "countdown", "approved", "focus", "phase", "client"}
FOCUS_KEYS = {"kind", "label", "title", "sub", "quote", "say", "fine", "tone"}
CHAT_KEYS = {"reply", "pending_draft", "tool_calls", "usage", "latency_ms", "model", "footer", "decision", "offer", "receipt"}
URL_RE = re.compile(r"https?://|www\.", re.I)
ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b", re.I)
CLAIM_RE = re.compile(r"\b(?:sent|done|saved|created)\b", re.I)
SPOKEN: list[tuple[str, str]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


TOM_BODY = ("Hi Alex,\nIs the 1,000 calls per day limit per user or per workspace? We are planning the rollout for next month. "
            "Could you also send the pricing sheet? Thanks, Tom\n\nOn Mon, 12 Oct 2026 at 10:00, Alex wrote:\n> Happy to help.")
RAVI_BODY = ("The design package for the launch site is ready, see https://files.example.org/pkg/7f3a9c2e1b. "
             "Can we walk through it on Thursday afternoon? Ping me at ravi.mehta@example.org if not.")
SAM_BODY = "Please find the October invoice: 18 design hours at the agreed rate. Could you confirm? " + "More detail here. " * 60


def setup():
    g, c, _tmr = tm.setup()
    g.msgs = {
        "a1": tm.fake_message("a1", "Tom Becker <tom.becker@example.org>", "Quick question on the API limits",
                              ["INBOX", "UNREAD", "CATEGORY_PERSONAL"], TOM_BODY),
        "a2": tm.fake_message("a2", "Ravi Mehta <ravi.mehta@example.org>", "Re: Design package: can we review it on Thursday?",
                              ["INBOX", "CATEGORY_PERSONAL"], RAVI_BODY),
        "a3": tm.fake_message("a3", "sam.lee@example.org", "Invoice 482 for October", ["INBOX", "CATEGORY_PERSONAL"], SAM_BODY),
        "u1": tm.fake_message("u1", "Cloudflare <em@em1.cloudflare.com>", "Connect is almost sold out",
                              ["INBOX", "UNREAD", "CATEGORY_UPDATES"], "Sale", {"List-Unsubscribe": "<mailto:u@x>"}),
    }
    c.ev = {}
    brief._cache.clear()
    voicestate.reset_all()
    return g, c


def add_event(c, eid, summary, start: datetime, minutes=30, attendees=(), location=""):
    c.ev[eid] = {"id": eid, "status": "confirmed", "summary": summary,
                 "start": {"dateTime": start.replace(second=0, microsecond=0).isoformat()},
                 "end": {"dateTime": (start + timedelta(minutes=minutes)).replace(second=0, microsecond=0).isoformat()},
                 "attendees": [{"email": a} for a in attendees], "location": location, "htmlLink": f"https://cal/{eid}"}


def say(session, vs, text):
    cmd = voicecmd.parse(text)
    t0 = time.perf_counter()
    r = fastpath.handle(session, cmd, vs)
    TIMES.append((text, (time.perf_counter() - t0) * 1000))
    if r is not None:
        SPOKEN.append((text, r["voice"]["spoken"]))
    return r


def well_formed(msgs) -> tuple[bool, str]:
    """Every tool message follows its call; every call is followed by its result."""
    for i, m in enumerate(msgs):
        if m.get("role") == "tool":
            prev = msgs[i - 1] if i else {}
            if not (prev.get("role") == "tool" or (prev.get("role") == "assistant" and prev.get("tool_calls"))):
                return False, f"orphan tool message at {i}"
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = msgs[i + 1] if i + 1 < len(msgs) else {}
            if nxt.get("role") != "tool":
                return False, f"call without result at {i}"
    return True, ""


def check_shape(name, r, intent):
    check(f"{name}: a /chat-shaped dict", isinstance(r, dict) and CHAT_KEYS <= set(r), sorted(r or {}))
    v = (r or {}).get("voice") or {}
    check(f"{name}: voice block has every contract key", set(v) == VOICE_KEYS, sorted(v))
    check(f"{name}: voice intent {intent}, phase listening, others null",
          v.get("intent") == intent and v.get("phase") == "listening" and v.get("readback") is None
          and v.get("countdown") is None and v.get("approved") is None and v.get("client") is None, v)
    if v.get("focus") is not None:
        check(f"{name}: focus card has every key", set(v["focus"]) == FOCUS_KEYS, sorted(v["focus"]))
    check(f"{name}: no model call", (r or {}).get("usage", {}).get("model_calls") == 0 and "no model call" in (r or {}).get("footer", ""),
          (r or {}).get("footer"))


# --------------------------------------------------------------------------- suites


def test_brief_walk():
    g, c = setup()
    s = agent.new_session()
    vs = voicestate.get(s.id)
    n0 = len(s.messages)
    r = say(s, vs, "Hey Vaani, brief me")
    check_shape("brief", r, "brief")
    v = r["voice"]
    check("brief: spoken like the mock-up",
          v["spoken"] == "Three emails need a reply. First, Tom Becker: Quick question on the API limits. Read it, reply, or next?",
          v["spoken"])
    f = v["focus"] or {}
    check("brief: focus card NOW · EMAIL 1 OF 3, Tom Becker",
          f.get("kind") == "email" and f.get("label") == "NOW · EMAIL 1 OF 3" and f.get("title") == "Tom Becker", f)
    check("brief: focus sub is 'subject · when'", f.get("sub", "").startswith("Quick question on the API limits · "), f.get("sub"))
    check("brief: focus quote is the snippet", f.get("quote") == " ".join(g.msgs["a1"]["snippet"].split()), f.get("quote"))
    check("brief: heard is the parsed text", v["heard"] == "brief me", v["heard"])
    walk = vs.data.get("fp.walk") or {}
    check("brief: walk stored under fp.walk", walk.get("ids") == ["a1", "a2", "a3"] and walk.get("i") == 0, walk)
    roles = [(m["role"], (m.get("tool_calls") or [{}])[0].get("function", {}).get("name") or m.get("tool_name")) for m in s.messages[n0:]]
    check("brief: transcript = user, listing call+result, read call+result, reply",
          roles == [("user", None), ("assistant", "list_recent_emails"), ("tool", "list_recent_emails"),
                    ("assistant", "read_email"), ("tool", "read_email"), ("assistant", None)], roles)
    check("brief: user line carries the [now:] tag", s.messages[n0]["content"].startswith("[now:") and s.messages[n0]["content"].endswith("brief me"))
    check("brief: the reply in the transcript is what the chat shows", s.messages[-1]["content"] == r["reply"])
    check("brief: email in focus is Tom's", (agent._email_in_focus(s) or {}).get("id") == "a1")
    check("brief: tool records for the live steps", [t["name"] for t in r["tool_calls"]] == ["read_email"], r["tool_calls"])
    ok, why = well_formed(s.messages)
    check("brief: transcript well-formed", ok, why)
    check("brief: rows from the walk are the needs-reply ones (no Cloudflare)", "u1" not in walk.get("ids", []))

    r = say(s, vs, "next")
    check_shape("next", r, "next")
    check("next: spoken 'Next, Ravi Mehta: <subject>' (Re: stripped, question kept)",
          r["voice"]["spoken"] == "Next, Ravi Mehta: Design package: can we review it on Thursday?", r["voice"]["spoken"])
    check("next: focus NOW · EMAIL 2 OF 3", (r["voice"]["focus"] or {}).get("label") == "NOW · EMAIL 2 OF 3", r["voice"]["focus"])
    check("next: email in focus is Ravi's", (agent._email_in_focus(s) or {}).get("id") == "a2")

    # "reply saying thanks" now answers Ravi's email (agent's code path; stand-in model writes the body)
    real = agent.llm.complete
    agent.llm.complete = lambda messages, tools_: SimpleNamespace(
        message=SimpleNamespace(content="Hi Ravi,\nThanks, Thursday works.\nAlex", tool_calls=[]), prompt_eval_count=10, eval_count=5)
    try:
        before = len(s.messages)
        res = agent.run_turn(s, "reply saying thanks")
    finally:
        agent.llm.complete = real
    pend = agent.pending_draft(s) or {}
    check("follow-up: 'reply saying thanks' makes a reply card to the email in focus",
          pend.get("kind") == "email_send" and pend.get("source_message_id") == "a2" and pend.get("mode") == "reply",
          {k: pend.get(k) for k in ("kind", "mode", "source_message_id", "to")})
    check("follow-up: the reply goes to Ravi", any("ravi.mehta@example.org" in str(t) for t in (pend.get("to") or [])), pend.get("to"))
    check("follow-up: nothing was sent", not g.sent_raw and "Nothing is sent" in res.reply, res.reply)
    ok, why = well_formed(s.messages)
    check("follow-up: transcript well-formed", ok, why)
    s.pending_draft_id = None  # leave the card; the walk continues

    r = say(s, vs, "Next one.")
    sp = r["voice"]["spoken"]
    check("next: last one, Sam Lee from the address, no address spoken", sp == "Last one, Sam Lee: Invoice 482 for October.", sp)
    check("next: focus EMAIL 3 OF 3 titled Sam Lee", (r["voice"]["focus"] or {}).get("title") == "Sam Lee"
          and (r["voice"]["focus"] or {}).get("label") == "NOW · EMAIL 3 OF 3", r["voice"]["focus"])
    r = say(s, vs, "read it")
    check_shape("read (long body)", r, "read")
    sp = r["voice"]["spoken"]
    check("read: '<name> wrote: ...' capped near 300 chars", sp.startswith("Sam wrote: ") or sp.startswith("Sam Lee wrote: "), sp)
    check("read: capped", len(sp) <= fastpath.SPOKEN_MAX + 10, len(sp))
    r = say(s, vs, "next")
    check("next past the end: the mock-up's line", r["voice"]["spoken"] == "That was the last one. Want your calendar instead?", r["voice"]["spoken"])
    check("next past the end: no focus card", r["voice"]["focus"] is None)
    r = say(s, vs, "next")
    check("next again past the end: same line, no crash", r["voice"]["spoken"] == "That was the last one. Want your calendar instead?")
    # read with the walk finished: the email still in focus (Sam's) is read
    r = say(s, vs, "read it")
    check("read after the walk: reads the email in focus", r["voice"]["spoken"].startswith("Sam"), r["voice"]["spoken"])
    ok, why = well_formed(s.messages)
    check("walk: transcript well-formed at the end", ok, why)


def test_read_rules():
    setup()
    s = agent.new_session()
    vs = voicestate.get(s.id)
    say(s, vs, "brief me")
    r = say(s, vs, "read it")
    sp = r["voice"]["spoken"]
    check("read: first two sentences, greeting and quoted reply left out",
          sp == "Tom wrote: Is the 1,000 calls per day limit per user or per workspace? We are planning the rollout for next month. "
                "Reply, or next?", sp)
    check("read: focus quote shows the body", "rollout" in (r["voice"]["focus"] or {}).get("quote", ""), r["voice"]["focus"])
    check("read: read_email recorded (live step)", [t["name"] for t in r["tool_calls"]] == ["read_email"])
    say(s, vs, "next")
    r = say(s, vs, "what does it say")
    sp = r["voice"]["spoken"]
    check("read: URL, id and address never spoken", not URL_RE.search(sp) and not ADDR_RE.search(sp) and not HEX_RE.search(sp), sp)
    check("read: Ravi's first sentences", sp.startswith("Ravi wrote: The design package for the launch site is ready"), sp)
    # no walk, nothing read: ask which one
    s2 = agent.new_session()
    vs2 = voicestate.get(s2.id)
    r = say(s2, vs2, "read it")
    check("read with nothing in focus: asks which email", r["voice"]["spoken"].startswith("Which email?"), r["voice"]["spoken"])
    r = say(s2, vs2, "next")
    check("next before a brief: the mock-up's line", r["voice"]["spoken"] == "Say “brief me” first and I will walk you through your inbox.",
          r["voice"]["spoken"])
    check("next before a brief: user line + reply kept", s2.messages[-2]["role"] == "user" and s2.messages[-1]["role"] == "assistant")


def test_brief_variants():
    g, c = setup()
    # brief already opened by flow.open_chat: no second listing in the transcript
    os.environ["VAANI_BRIEF_ON_OPEN"] = "1"
    try:
        s = agent.new_session()
        flow.open_chat(s)
    finally:
        os.environ["VAANI_BRIEF_ON_OPEN"] = "0"
    vs = voicestate.get(s.id)
    r = say(s, vs, "what's in my inbox")
    listings = sum(1 for m in s.messages if m.get("role") == "tool" and m.get("tool_name") == "list_recent_emails")
    check("brief after open_chat: uses session.brief, one listing in the transcript", listings == 1, listings)
    check("brief after open_chat: same spoken line", r["voice"]["spoken"].startswith("Three emails need a reply."), r["voice"]["spoken"])
    # one email
    setup()
    tm_g = google_auth.get_services()["gmail"]
    for mid in ("a2", "a3"):
        tm_g.msgs.pop(mid)
    s = agent.new_session()
    vs = voicestate.get(s.id)
    r = say(s, vs, "brief me")
    check("brief with one email: singular line",
          r["voice"]["spoken"] == "One email needs a reply. Tom Becker: Quick question on the API limits. Read it, or reply?",
          r["voice"]["spoken"])
    # none
    setup()
    tm_g = google_auth.get_services()["gmail"]
    for mid in ("a1", "a2", "a3"):
        tm_g.msgs.pop(mid)
    s = agent.new_session()
    vs = voicestate.get(s.id)
    r = say(s, vs, "brief me")
    check("brief with nothing to answer: says so, offers the calendar",
          r["voice"]["spoken"].startswith("No emails need a reply right now.") and r["voice"]["spoken"].endswith("Want your calendar instead?"),
          r["voice"]["spoken"])
    check("brief with nothing: walk empty", (vs.data.get("fp.walk") or {}).get("ids") == [])
    # Gmail down
    setup()
    real = google_auth.get_services
    google_auth.get_services = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    try:
        s = agent.new_session()
        vs = voicestate.get(s.id)
        n0 = len(s.messages)
        r = say(s, vs, "brief me")
    finally:
        google_auth.get_services = real
    check("brief, Gmail down: honest line", r["voice"]["spoken"].startswith("I couldn't read your inbox just now."), r["voice"]["spoken"])
    check("brief, Gmail down: user line + reply only", [m["role"] for m in s.messages[n0:]] == ["user", "assistant"])


def test_calendar():
    g, c = setup()
    tz = tools._tz()
    now = datetime.now(tz)
    base = now.replace(second=0, microsecond=0)  # minute-aligned: 18 min ahead is said "in 18 minutes"
    soon = base + timedelta(minutes=18)
    later = base + timedelta(hours=3)
    earlier = base - timedelta(hours=3)
    add_event(c, "e1", "Design review", soon, 30, ["ravi.mehta@example.org", "paula.sanders@example.org", "sam.lee@example.org",
                                                    tm.OWN], "Google Meet")
    add_event(c, "e2", "Weekly team sync", later, 60)
    add_event(c, "e0", "Standup", earlier, 15)
    today = now.date()
    on_today = [e for e in (earlier, soon, later) if e.date() == today]
    upcoming_today = [e for e in (soon, later) if e.date() == today]
    s = agent.new_session()
    vs = voicestate.get(s.id)
    n0 = len(s.messages)
    r = say(s, vs, "What's on my calendar today?")
    check_shape("calendar_today", r, "calendar_today")
    sp, f = r["voice"]["spoken"], r["voice"]["focus"] or {}
    roles = [m["role"] for m in s.messages[n0:]]
    check("calendar_today: transcript = user, call, result, reply", roles == ["user", "assistant", "tool", "assistant"], roles)
    check("calendar_today: list_calendar_events for today recorded",
          s.messages[n0 + 1]["tool_calls"][0]["function"] == {"name": "list_calendar_events", "arguments": {"on_date": "today"}})
    n = len(on_today)
    if soon.date() == today:
        check("calendar_today: count and next meeting spoken",
              sp.startswith(f"You have {fastpath._num(n)} meeting") and "Next is Design review at " in sp and "in 18 minutes" in sp, sp)
        check("calendar_today: focus meeting card like the mock-up",
              f.get("kind") == "meeting" and f.get("label") == f"TODAY · {n} MEETING{'S' if n != 1 else ''}"
              and f.get("title") == f"{soon:%H:%M} · Design review" and f.get("sub", "").startswith("in 18 min · Google Meet · 4 people"), f)
        if later.date() == today:
            check("calendar_today: then the rest", "Then Weekly team sync at " in sp, sp)
        if earlier.date() == today:
            check("calendar_today: past meetings counted, still-to-come said", f"{fastpath._num(len(upcoming_today))} still to come" in sp, sp)
    check("calendar_today: no address in spoken or focus", not ADDR_RE.search(sp) and not ADDR_RE.search(json.dumps(f)), (sp, f))

    r = say(s, vs, "When is my next meeting?")
    check_shape("next_meeting", r, "next_meeting")
    sp, f = r["voice"]["spoken"], r["voice"]["focus"] or {}
    check("next_meeting: spoken", sp.startswith("Your next meeting is Design review ") and "in 18 minutes" in sp
          if soon.date() == today else sp.startswith("Your next meeting is Design review tomorrow"), sp)
    check("next_meeting: focus NEXT MEETING", f.get("kind") == "meeting" and f.get("label") == "NEXT MEETING" and "Design review" in f.get("title", ""), f)
    check("next_meeting: days_ahead 7 call recorded",
          s.messages[-3]["tool_calls"][0]["function"] == {"name": "list_calendar_events", "arguments": {"days_ahead": 7}})
    # in progress + tomorrow. Google returns events that overlap timeMin; the shared fake only
    # returns those starting after it, so this check lists like Google does.
    def google_list(calendarId="primary", timeMin=None, timeMax=None, **_kw):
        lo, hi = datetime.fromisoformat(timeMin), datetime.fromisoformat(timeMax)
        items = [copy.deepcopy(e) for e in c.ev.values() if datetime.fromisoformat(e["end"]["dateTime"]) > lo
                 and datetime.fromisoformat(e["start"]["dateTime"]) < hi]
        return tm.Req(lambda: {"items": sorted(items, key=lambda e: e["start"]["dateTime"])})

    c.list = google_list
    c.ev = {}
    add_event(c, "e3", "Client call", now - timedelta(minutes=10), 40)
    add_event(c, "e4", "Lunch with Paula", now + timedelta(days=1), 60)
    r = say(s, vs, "next meeting")
    sp = r["voice"]["spoken"]
    check("next_meeting: on now, then tomorrow", sp.startswith("Client call is on now, until ") and "Lunch with Paula tomorrow at " in sp, sp)
    c.ev = {}
    r = say(s, vs, "next meeting")
    check("next_meeting: empty week", r["voice"]["spoken"] == "Nothing on your calendar in the next seven days.", r["voice"]["spoken"])
    r = say(s, vs, "my day")
    check("calendar_today: clear day", r["voice"]["spoken"] == "Your calendar is clear today.", r["voice"]["spoken"])
    # calendar down: honest line, transcript coherent
    real = google_auth.get_services
    google_auth.get_services = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    try:
        n0 = len(s.messages)
        r = say(s, vs, "what's on my calendar")
    finally:
        google_auth.get_services = real
    check("calendar down: honest line", r["voice"]["spoken"].startswith("I couldn't read your calendar just now."), r["voice"]["spoken"])
    ok, why = well_formed(s.messages)
    check("calendar down: transcript well-formed (error result kept with its call)", ok and len(s.messages) - n0 == 4, why)


def test_handle_edges():
    setup()
    s = agent.new_session()
    vs = voicestate.get(s.id)
    for text in ("confirm", "send it", "stop", "reply saying thanks", "goodnight", "set replies to auto", ""):
        check(f"handle returns None for {text!r}", fastpath.handle(s, voicecmd.parse(text), vs) is None)
    check("handle without a voice state returns None", fastpath.handle(s, voicecmd.parse("brief me"), None) is None)
    n0 = len(s.messages)
    real = agent._seed_tool_call
    agent._seed_tool_call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        r = fastpath.handle(s, voicecmd.parse("what's on my calendar"), vs)
    finally:
        agent._seed_tool_call = real
    check("an unexpected error: None (the caller falls back), transcript unchanged", r is None and len(s.messages) == n0)


def test_spoken_rules():
    for text, sp in SPOKEN:
        bad = []
        if URL_RE.search(sp):
            bad.append("url")
        if ADDR_RE.search(sp):
            bad.append("address")
        if HEX_RE.search(sp):
            bad.append("id")
        if re.search(r"\*\*|__|`|\]\(|^\s*#|^\s*[-*] |\n", sp):
            bad.append("markdown")
        if CLAIM_RE.search(sp):
            bad.append("claim")
        if not sp.strip():
            bad.append("empty")
        check(f"spoken rules ({text!r}): {sp[:50]!r}", not bad, bad)
    worst = max((ms for _t, ms in TIMES), default=0)
    print(f"fast path code time with fakes: worst {worst:.1f} ms, mean {sum(ms for _t, ms in TIMES) / max(1, len(TIMES)):.1f} ms")
    check(f"code time with fakes < 300 ms per fast path (worst {worst:.0f} ms over {len(TIMES)} calls)", worst < 300, TIMES)


def test_greeting():
    g, c = setup()
    tz = tools._tz()
    now = datetime.now(tz)
    word = fastpath._greet_word(now)
    s = agent.new_session()
    soon = now.replace(second=0, microsecond=0) + timedelta(minutes=18)
    add_event(c, "e1", "Design review", soon, 30)
    text = fastpath.greeting(s, "Alex Doe")
    if soon.date() == now.date():
        want = f"{word}, Alex. Three emails need a reply, and your design review is in 18 minutes. Where shall we start?"
        check("greeting: like the mock-up (first name, counts, meeting in N minutes)", text == want, text)
    check("greeting: first name only", "Doe" not in text, text)
    c.ev = {}
    add_event(c, "e2", "Design review", now + timedelta(hours=3), 30)
    text = fastpath.greeting(s, "Alex")
    check("greeting: meeting beyond 2 hours omitted", text == f"{word}, Alex. Three emails need a reply. Where shall we start?", text)
    c.ev = {}
    add_event(c, "e5", "Ravi / Alex sync", now.replace(second=0, microsecond=0) + timedelta(minutes=40), 30)
    text = fastpath.greeting(s, "Alex")
    if (now + timedelta(minutes=40)).date() == now.date():
        check("greeting: a title that is not a common word stays as it is", "Ravi / Alex sync is in 40 minutes" in text, text)
    check("greeting: no name", fastpath.greeting(s, "").startswith(f"{word}. "), fastpath.greeting(s, ""))
    check("greeting: an address is never used as a name", "@" not in fastpath.greeting(s, "sam.lee@example.org"))
    real = google_auth.get_services
    google_auth.get_services = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    brief._cache.clear()
    try:
        s2 = agent.new_session()
        text = fastpath.greeting(s2, "Alex")
    finally:
        google_auth.get_services = real
    check("greeting: Google down -> still a greeting", text == f"{word}, Alex. Where shall we start?", text)
    check("greeting: does not touch the transcript", len(s2.messages) == 1)
    check("greeting words by hour", [fastpath._greet_word(now.replace(hour=h)) for h in (3, 9, 13, 19)]
          == ["Hello", "Good morning", "Good afternoon", "Good evening"])


# --------------------------------------------------------------------------- voicectx.trim


def long_transcript(turns=10) -> list[dict]:
    msgs = [{"role": "system", "content": "You are Vaani."}]
    for t in range(turns):
        msgs.append({"role": "user", "content": f"[now: 2026-10-14T12:{t:02d} Asia/Kolkata] question {t}"})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "list_recent_emails", "arguments": {"max_results": 20}}},
            {"function": {"name": "read_email", "arguments": {"message_id": f"m{t}"}}}]})
        msgs.append({"role": "tool", "content": json.dumps({"messages": [{"id": f"m{t}", "snippet": "x" * 300}] * 10}), "tool_name": "list_recent_emails"})
        msgs.append({"role": "tool", "content": "body " * 400, "tool_name": "read_email"})
        msgs.append({"role": "assistant", "content": f"answer {t}"})
        if t % 3 == 0:  # a Confirm click inside the turn (synthetic user line)
            msgs.append({"role": "user", "content": f"[now: 2026-10-14T12:{t:02d} Asia/Kolkata] User clicked Cancel on card d{t}. Do not do it."})
            msgs.append({"role": "assistant", "content": "OK, nothing was changed."})
    return msgs


def test_trim():
    msgs = long_transcript(10)
    snapshot = copy.deepcopy(msgs)
    os.environ.pop("VAANI_VOICE_CTX_TURNS", None)
    out = voicectx.trim(msgs)
    check("trim: input not mutated", msgs == snapshot)
    check("trim: a new list", out is not msgs and all(o is not m for o in out for m in msgs))
    check("trim: system first, style line appended once",
          out[0]["role"] == "system" and out[0]["content"].endswith(voicectx.STYLE) and out[0]["content"].count(voicectx.STYLE) == 1,
          out[0]["content"])
    check("trim: style line is the contract's",
          voicectx.STYLE == "Voice mode: answer in one or two short spoken sentences; no lists, links or markdown.")
    users = [m for m in out if m["role"] == "user" and "User clicked" not in m["content"]]
    check("trim: last 3 user turns kept", [u["content"][-10:] for u in users] == ["question 7", "question 8", "question 9"],
          [u["content"] for u in users])
    check("trim: starts with a user turn after the system message", out[1]["role"] == "user")
    ok, why = well_formed(out)
    check("trim: well-formed for Ollama", ok, why)
    last_user = max(i for i, m in enumerate(out) if m["role"] == "user" and "User clicked" not in m["content"])
    old_tools = [m for i, m in enumerate(out) if m["role"] == "tool" and i < last_user]
    new_tools = [m for i, m in enumerate(out) if m["role"] == "tool" and i > last_user]
    check("trim: older tool results shortened to ~800 chars", old_tools and all(len(m["content"]) <= 840 for m in old_tools),
          [len(m["content"]) for m in old_tools])
    orig_last = max(i for i, m in enumerate(snapshot) if m["role"] == "user" and "User clicked" not in m["content"])
    orig_new = [m["content"] for m in snapshot[orig_last:] if m["role"] == "tool"]
    check("trim: the latest turn's tool results intact", [m["content"] for m in new_tools] == orig_new and len(orig_new) == 2,
          [len(m["content"]) for m in new_tools])
    check("trim: tool_calls kept on assistant messages", sum(1 for m in out if m.get("tool_calls")) == 3)
    check("trim: much shorter", len(json.dumps(out)) < len(json.dumps(msgs)) / 2, (len(json.dumps(out)), len(json.dumps(msgs))))
    os.environ["VAANI_VOICE_CTX_TURNS"] = "1"
    out1 = voicectx.trim(msgs)
    check("trim: VAANI_VOICE_CTX_TURNS=1 keeps one turn (with its Cancel click)",
          [m["content"][-10:] for m in out1 if m["role"] == "user" and "User clicked" not in m["content"]] == ["question 9"]
          and out1[1]["content"].endswith("question 9"), [m["content"] for m in out1 if m["role"] == "user"])
    os.environ["VAANI_VOICE_CTX_TURNS"] = "lots"
    check("trim: a bad VAANI_VOICE_CTX_TURNS falls back to 3", len([m for m in voicectx.trim(msgs) if m["role"] == "user"
                                                                   and "User clicked" not in m["content"]]) == 3)
    os.environ.pop("VAANI_VOICE_CTX_TURNS", None)
    short = long_transcript(2)
    out2 = voicectx.trim(short)
    check("trim: a short transcript keeps every turn", len(out2) == len(short), (len(out2), len(short)))
    check("trim: empty in, empty out", voicectx.trim([]) == [])
    nosys = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    out3 = voicectx.trim(nosys)
    check("trim: no system message -> one added with the style line", out3[0] == {"role": "system", "content": voicectx.STYLE}
          and out3[1:] == nosys, out3)
    broken = [{"role": "system", "content": "S"}, {"role": "user", "content": "q"}, {"role": "tool", "content": "orphan", "tool_name": "x"},
              {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_email", "arguments": {}}}]}]
    out4 = voicectx.trim(broken)
    ok, why = well_formed(out4)
    check("trim: an orphan tool result and a call without result are dropped", ok and len(out4) == 2, out4)
    # a confirm-in-the-turn synthetic line does not count as a turn
    msgs5 = long_transcript(4)
    out5 = voicectx.trim(msgs5)
    check("trim: synthetic 'User clicked' lines are not counted as turns",
          [m["content"][-10:] for m in out5 if m["role"] == "user" and "User clicked" not in m["content"]]
          == ["question 1", "question 2", "question 3"])
    # integration: agent._Loop.run hands the trimmed list to the model in voice mode only
    seen = []
    real = agent.llm.complete
    agent.llm.complete = lambda m, t: (seen.append(m), SimpleNamespace(
        message=SimpleNamespace(content="Sure.", tool_calls=[]), prompt_eval_count=1, eval_count=1))[1]
    try:
        sess = agent.new_session()
        sess.messages += copy.deepcopy(long_transcript(6)[1:])
        sess.messages.append({"role": "user", "content": "[now: x] hello"})
        tok = obs.current_mode.set("voice")
        try:
            agent._Loop(sess).run(1)
        finally:
            obs.current_mode.reset(tok)
        voice_seen = seen[-1]
        agent._Loop(sess).run(1)
        chat_seen = seen[-1]
    finally:
        agent.llm.complete = real
    check("integration: voice mode sends the trimmed context", voice_seen[0]["content"].endswith(voicectx.STYLE)
          and len(voice_seen) < len(sess.messages), len(voice_seen))
    check("integration: chat mode is unchanged", chat_seen is sess.messages or chat_seen == sess.messages[:len(chat_seen)])
    check("integration: session.messages never got the style line", voicectx.STYLE not in sess.messages[0]["content"])


def main() -> int:
    test_brief_walk()
    test_read_rules()
    test_brief_variants()
    test_calendar()
    test_handle_edges()
    test_greeting()
    test_spoken_rules()
    test_trim()
    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL {name}: {detail[:600]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} fastpath checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
