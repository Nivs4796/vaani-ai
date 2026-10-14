"""Offline checks for the voice-mode routes (assistant/routes_voiceturn.py): /voice/turn as
an SSE stream, /voice/commit, /voice/disarm, /voice/mode and /voice/greeting, end to end
through FastAPI's TestClient, against the fake Gmail and Calendar of evals.test_manage.

Every refusal case asserts that NOTHING was written to the fakes. The model is never
called (llm.complete raises); where a model turn would run, flow.chat is a fake.
Transcripts are issued with voicestate.issue_transcript, the way /voice/stt does, and
read-backs are recorded with voicestate.start_speech / end_speech, the way /voice/tts and
/voice/played do. The voicestate clock is patched. Made-up people only.
Run: .venv/bin/python -m evals.test_voiceturn   (exit 1 on any failure)
"""

from __future__ import annotations

import json
import os
import sys

from fastapi.testclient import TestClient

from assistant import agent, autonomy, flow, manage, obs, tools, voicestate, web
from assistant import voice_policy as vp
from evals import test_manage as tm
from evals import test_voice_policy as tvp
from evals.test_voice_policy import CLOCK, check, in_session, read_back, reply_card, writes

RESULTS = tvp.RESULTS
REMEMBERED: list[tuple] = []
SEEN: dict = {}
G = C = None
CLIENT: TestClient | None = None


async def fake_remember(request, session_id, kind, req, res):
    REMEMBERED.append((session_id, kind, req, res))


def fake_model_chat(session, text):
    """Stands in for a model turn: its reply even says "confirm" and "send it", which must change nothing."""
    SEEN["mode"] = obs.current_mode.get()
    SEEN["text"] = text
    session.messages.append({"role": "user", "content": f"{agent._now_tag()} {text}"})
    reply = "I can confirm the plan. Say send it when ready, it was sent already."
    session.messages.append({"role": "assistant", "content": reply})
    return flow._out(agent._result(session, reply, [], agent._zero_usage(), 0.0))


# --------------------------------------------------------------------------- helpers


def events_of(r) -> list[dict]:
    out = []
    for block in r.text.split("\n\n"):
        if block.strip():
            out.append(json.loads(block[len("data: "):]))
    return out


def new_voice_session():
    sid = CLIENT.post("/new").json()["session_id"]
    r = CLIENT.post("/voice/mode", json={"session_id": sid, "mode": "voice"})
    assert r.status_code == 200 and r.json()["mode"] == "voice", r.text
    return sid, web.SESSIONS[sid].session


def issue(sid, text, conf=0.92, gap=1.0, overlap=False, user_id=None):
    CLOCK.t += gap
    t = voicestate.issue_transcript(sid, user_id, text, conf, CLOCK.t, CLOCK.t + 0.8, client_overlap=overlap)
    CLOCK.t += 0.9
    return t.tid


def turn(sid, tid) -> dict:
    r = CLIENT.post("/voice/turn", json={"session_id": sid, "transcript_id": tid})
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("text/event-stream"):
        return {"_status": r.status_code, **(r.json() if r.content else {})}
    evs = events_of(r)
    res = next((e["result"] for e in evs if e["type"] == "result"), None) or {}
    res["_types"] = [e["type"] for e in evs]
    return res


def say(sid, text, **kw) -> dict:
    return turn(sid, issue(sid, text, **kw))


def commit(sid, arm_id):
    r = CLIENT.post("/voice/commit", json={"session_id": sid, "arm_id": arm_id})
    return r.status_code, r.json()


def W():
    return writes(G, C)


def sends():
    return len(G.sent_raw)


def voice_of(res) -> dict:
    return res.get("voice") or {}


def trash_card(session, ids=("m2",)):
    tm.user(session, "trash the Cloudflare email")
    r = tm.j(in_session(session, manage.modify_emails, "trash", message_ids=list(ids)))
    return session.drafts[r["draft_id"]]


def archive_card(session, ids=("m3",)):
    tm.user(session, "archive the Atlassian email")
    r = tm.j(in_session(session, manage.modify_emails, "archive", message_ids=list(ids)))
    return session.drafts[r["draft_id"]]


def event_card(session, attendees=()):
    tz = tools._tz()
    from datetime import datetime, timedelta

    start = (datetime.now(tz) + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
    d = {"draft_id": f"d{len(session.drafts) + 7:04x}", "summary": "Design sync with Paula Sanders", "start": start.isoformat(),
         "end": (start + timedelta(minutes=30)).isoformat(), "tz": str(tz), "attendees": list(attendees), "description": "",
         "status": "DRAFT"}
    in_session(session, autonomy.tag_draft, d, None)
    session.drafts[d["draft_id"]] = d
    session.pending_draft_id = d["draft_id"]
    return d


def armed(sid, session, card_fn=reply_card, word="send it"):
    """A pending card, read back, then the tier-2 word: returns (card, result, arm_id)."""
    card = card_fn(session)
    read_back(sid, card["draft_id"])
    res = say(sid, word)
    cd = voice_of(res).get("countdown") or {}
    return card, res, cd.get("arm_id")


def past_deadline():
    CLOCK.t += voicestate.COUNTDOWN_S + 0.2


# --------------------------------------------------------------------------- checks


def check_modes_and_typed():
    sid = CLIENT.post("/new").json()["session_id"]
    s = web.SESSIONS[sid].session
    d = reply_card(s)
    tid = issue(sid, "confirm")
    r = turn(sid, tid)
    check("chat mode: /voice/turn answers 409 voice mode is off", r.get("_status") == 409 and r.get("error") == "voice mode is off", r)
    before = W()
    plain = CLIENT.post("/chat", json={"session_id": sid, "text": "confirm"}).json()
    check("chat mode keeps today's rules: typed confirm is a pointer", "click Send" in plain["reply"] and W() == before, plain["reply"])
    CLIENT.post("/voice/mode", json={"session_id": sid, "mode": "voice"})
    for text in ("confirm", "send it"):
        r1 = CLIENT.post("/chat", json={"session_id": sid, "text": text, "via": "typed"}).json()
        evs = events_of(CLIENT.post("/chat/stream", json={"session_id": sid, "text": text}))
        r2 = next(e["result"] for e in evs if e["type"] == "result")
        check(f"voice mode: typed '{text}' via /chat and /chat/stream writes nothing",
              "click Send" in r1["reply"] and "click Send" in r2["reply"] and W() == before and s.pending_draft_id == d["draft_id"],
              (r1["reply"], r2["reply"]))
    r = CLIENT.post("/voice/mode", json={"session_id": sid, "mode": "loud"})
    check("a bad mode is a 422", r.status_code == 422, r.text)
    r = CLIENT.post("/voice/mode", json={"session_id": "nope1234", "mode": "voice"})
    check("an unknown session is a 404", r.status_code == 404, r.text)
    g = CLIENT.get(f"/voice/greeting?session_id={sid}")
    check("greeting returns text", g.status_code == 200 and isinstance(g.json().get("text"), str) and g.json()["text"], g.text)
    w = CLIENT.get(f"/voice/card?session_id={sid}").json()
    check("/voice/card: the waiting card's focus and read-back, from the server's copy",
          w.get("draft_id") == d["draft_id"] and (w.get("focus") or {}).get("kind") == "approval"
          and (w.get("readback") or {}).get("draft_id") == d["draft_id"] and "confirm" in w.get("spoken", "") and W() == before, w)
    check("/voice/card for an unknown session is a 404", CLIENT.get("/voice/card?session_id=nope1234").status_code == 404)
    CLIENT.post("/chat", json={"session_id": sid, "cancel_draft_id": d["draft_id"]})
    check("/voice/card with nothing waiting: draft_id null", CLIENT.get(f"/voice/card?session_id={sid}").json() == {"draft_id": None})


def check_refusals():
    sid, s = new_voice_session()
    d = reply_card(s)
    did = d["draft_id"]
    before = W()

    r = say(sid, "confirm")
    vb = voice_of(r)
    check("no read-back yet: Vaani reads the card back, nothing written",
          W() == before and (vb.get("readback") or {}).get("draft_id") == did and vb.get("spoken") == vp.readback_text(d)
          and r["_types"][-2:] == ["result", "done"], vb)
    check("the result is the /chat dict plus voice", {"reply", "pending_draft", "receipt", "voice"} <= set(r)
          and set(vb) >= {"intent", "heard", "confidence", "spoken", "readback", "countdown", "approved", "focus", "phase", "client"}, sorted(r))

    read_back(sid, did, complete=False)
    r = say(sid, "confirm")
    check("an interrupted read-back: read back again, nothing written", W() == before and voice_of(r).get("readback"), voice_of(r))

    read_back(sid, did)
    CLOCK.t += voicestate.READBACK_TTL_S + 1
    r = say(sid, "confirm")
    check("a read-back older than 30 s: read back again, nothing written", W() == before and voice_of(r).get("readback"), voice_of(r))

    read_back(sid, did)
    r = say(sid, "confirm", conf=0.3)
    check("low confidence: fixed reason, nothing written", W() == before and voice_of(r).get("spoken") == vp.REASONS["low_conf"], voice_of(r))

    read_back(sid, did)
    r = say(sid, "confirm", overlap=True)
    check("Vaani hearing itself (overlap flag): refused", W() == before and voice_of(r).get("spoken") == vp.REASONS["overlap"], voice_of(r))

    sp = voicestate.start_speech(sid, "readback", 5000, draft_id=did, text="Can you confirm the quote by Friday? Say confirm ...")
    tid = issue(sid, "confirm", gap=0.5)  # captured while the read-back with "confirm" in it was playing
    voicestate.end_speech(sid, sp.speak_id, True)
    r = turn(sid, tid)
    check("'confirm' inside an email read aloud (capture overlaps the read-back): refused",
          W() == before and voice_of(r).get("spoken") == vp.REASONS["overlap"], voice_of(r))

    read_back(sid, did)
    tid = issue(sid, "confirm")
    turn(sid, tid)
    before = W()
    r = turn(sid, tid)
    check("a reused transcript id: refused", W() == before and voice_of(r).get("intent") == "stale", voice_of(r))
    if s.pending_draft_id != did:  # the first use went through: new card for the rest
        d = reply_card(s)
        did = d["draft_id"]
        before = W()

    read_back(sid, did)
    tid = issue(sid, "confirm")
    CLOCK.t += voicestate.TRANSCRIPT_TTL_S + 1
    r = turn(sid, tid)
    check("a transcript older than 15 s: refused", W() == before and voice_of(r).get("intent") == "stale", voice_of(r))

    sid2, s2 = new_voice_session()
    read_back(sid, did)
    tid = issue(sid2, "confirm")
    r = turn(sid, tid)
    check("another session's transcript: refused", W() == before and voice_of(r).get("intent") == "stale", voice_of(r))
    read_back(sid, did)
    tid = issue(sid, "confirm", user_id="another-user")
    r = turn(sid, tid)
    check("another user's transcript: refused", W() == before and voice_of(r).get("intent") == "stale", voice_of(r))

    read_back(sid, did)
    r = say(sid, "trash it")
    check("a word that is not this card's: says the right words, nothing written",
          W() == before and "confirm" in voice_of(r).get("spoken", "") and s.pending_draft_id == did, voice_of(r))

    read_back(sid, did)
    d2 = reply_card(s)  # read back A, then B pending (A superseded)
    r = say(sid, "confirm")
    check("wrong card (read back A, then B pending): B is read back, nothing written",
          W() == before and (voice_of(r).get("readback") or {}).get("draft_id") == d2["draft_id"]
          and s.drafts[did]["status"] == "SUPERSEDED", voice_of(r))

    read_back(sid, d2["draft_id"])
    r = say(sid, "don't confirm")
    check("'don't confirm' writes nothing (it is a cancel)", W() == before and s.drafts[d2["draft_id"]]["status"] == "CANCELLED", voice_of(r))
    r = say(sid, "confirm")
    check("a cancelled card: nothing waiting, nothing written", W() == before and voice_of(r).get("spoken") == vp.NOTHING_WAITING, voice_of(r))

    r = say(sid, "set replies to auto")
    check("screen-only phrase: the fixed line, nothing written", W() == before and voice_of(r).get("spoken") == vp.SCREEN_ONLY, voice_of(r))

    d3 = reply_card(s)
    read_back(sid, d3["draft_id"])
    r = say(sid, "yes")
    check("bare yes never approves", W() == before and voice_of(r).get("spoken") == "Say confirm to go ahead.", voice_of(r))

    flow.chat = fake_model_chat
    try:
        r = say(sid, "what does Ravi want")
        check("a model reply that says confirm and send it writes nothing", W() == before and s.pending_draft_id == d3["draft_id"]
              and "sent" not in voice_of(r).get("spoken", "").lower(), voice_of(r))
        check("the model turn ran with obs.current_mode voice", SEEN.get("mode") == "voice" and SEEN.get("text") == "what does Ravi want", SEEN)
        check("a card already read back is not read again by a model turn (one reminder instead)",
              voice_of(r).get("readback") is None and vp.STILL_WAITING in voice_of(r).get("spoken", "")
              and voice_of(r).get("phase") == "listening" and "tap" not in voice_of(r).get("spoken", "").lower(), voice_of(r))

        def fake_new_card(session, text):  # the model drafts a NEW card this turn
            fake_model_chat(session, text)
            reply_card(session)
            return flow._out(agent._result(session, "Here is the reply.", [], agent._zero_usage(), 0.0))

        flow.chat = fake_new_card
        r = say(sid, "reply saying Thursday works")
        new_id = s.pending_draft_id
        check("a model turn that makes a new card reads THAT card back", new_id != d3["draft_id"]
              and (voice_of(r).get("readback") or {}).get("draft_id") == new_id and voice_of(r).get("phase") == "approval"
              and W() == before, voice_of(r))
    finally:
        flow.chat = REAL_CHAT
    rows = [x for x in REMEMBERED if x[0] == sid and x[1] == "chat"]
    check("voice turns are recorded as chat history with via voice, tid and confidence",
          rows and rows[-1][2].get("via") == "voice" and rows[-1][2].get("tid") and rows[-1][2].get("confidence") is not None, rows[-1:])
    check("the heard line and reply are in the transcript", any(m.get("role") == "user" and m.get("content", "").endswith(" set replies to auto")
                                                                for m in s.messages))


def check_done_cards():
    sid, s = new_voice_session()
    a = archive_card(s)
    read_back(sid, a["draft_id"])
    r = say(sid, "send it")
    before = W()
    check("tier-2 word on a tier-1 card: refused", W() == before and "confirm" in voice_of(r).get("spoken", "")
          and s.pending_draft_id == a["draft_id"], voice_of(r))
    read_back(sid, a["draft_id"])
    r = say(sid, "confirm")
    vb = voice_of(r)
    check("archive by voice: carried out at once, verified", "INBOX" not in G.msgs["m3"]["labelIds"] and r["receipt"]["verified"]
          and vb.get("approved", {}).get("by") == "voice" and vb.get("phase") == "done", vb)
    after = W()
    r = say(sid, "confirm")
    check("an already-done card: nothing waiting, written once", W() == after and voice_of(r).get("spoken") == vp.NOTHING_WAITING, voice_of(r))
    r = say(sid, "undo")
    check("voice undo acts at once like the receipt's Undo", "INBOX" in G.msgs["m3"]["labelIds"] and voice_of(r).get("intent") == "undo"
          and (r.get("receipt") or {}).get("verified"), voice_of(r))

    t = trash_card(s)
    read_back(sid, t["draft_id"])
    before = W()
    r = say(sid, "confirm")
    check("tier-1 word on a tier-2 card (trash): refused, says trash it", W() == before and "trash it" in voice_of(r).get("spoken", ""), voice_of(r))
    read_back(sid, t["draft_id"])
    r = say(sid, "trash it")
    cd = voice_of(r).get("countdown") or {}
    check("trash it starts the countdown, nothing written yet", W() == before and cd.get("seconds") == 5 and voice_of(r).get("phase") == "countdown",
          voice_of(r))
    past_deadline()
    code, out = commit(sid, cd.get("arm_id"))
    check("trash commit at zero: moved to Trash, verified", code == 200 and "TRASH" in G.msgs["m2"]["labelIds"]
          and out["receipt"]["verified"] and "Trash" in voice_of(out).get("spoken", ""), (code, voice_of(out)))

    ev = event_card(s)
    read_back(sid, ev["draft_id"])
    n = len(C.inserted)
    r = say(sid, "confirm")
    check("event without guests by confirm: created, verified", len(C.inserted) == n + 1 and (r.get("receipt") or {}).get("verified")
          and "confirmed it" in voice_of(r).get("spoken", ""), voice_of(r))
    ev2 = event_card(s, ["sam.lee@example.org"])
    read_back(sid, ev2["draft_id"])
    r = say(sid, "confirm")
    check("event with guests: confirm is not enough", len(C.inserted) == n + 1 and "invite them" in voice_of(r).get("spoken", ""), voice_of(r))
    read_back(sid, ev2["draft_id"])
    r = say(sid, "invite them")
    cd = voice_of(r).get("countdown") or {}
    past_deadline()
    code, out = commit(sid, cd.get("arm_id"))
    check("event with guests: invite them, countdown, commit creates it once", code == 200 and len(C.inserted) == n + 2, (code, out.get("reply")))

    tm.user(s, "delete the call with Andy Patel")
    r0 = tm.j(in_session(s, manage.draft_event_change, "delete", event_id="e3"))
    read_back(sid, r0["draft_id"])
    r = say(sid, "confirm")
    check("delete an event without guests by confirm", C.ev["e3"]["status"] == "cancelled" and (r.get("receipt") or {}).get("verified"), voice_of(r))
    tm.user(s, "delete the call with Andy Chen")
    r0 = tm.j(in_session(s, manage.draft_event_change, "delete", event_id="e1"))
    read_back(sid, r0["draft_id"])
    r = say(sid, "confirm")
    check("delete an event with guests: confirm refused", C.ev["e1"]["status"] == "confirmed" and "delete it" in voice_of(r).get("spoken", ""),
          voice_of(r))


def check_email_flow():
    log = tvp.fresh_log()
    sid, s = new_voice_session()
    d = reply_card(s)
    read_back(sid, d["draft_id"])
    n_sent = sends()
    r = say(sid, "confirm")
    vb = voice_of(r)
    card = r.get("pending_draft") or {}
    check("confirm saves the reply to Gmail drafts (verified), nothing sent", len(G.drafts_made) >= 1 and sends() == n_sent
          and r["receipt"]["status"] == "SAVED_DRAFT" and r["receipt"]["verified"], r.get("receipt"))
    check("then the saved-draft send card is pending and read back", card.get("gmail_draft_id") and card["card"]["title"] == "Send the saved draft"
          and (vb.get("readback") or {}).get("draft_id") == card.get("draft_id") and vb.get("spoken") == vp.SAVED_LINE
          and vb.get("focus", {}).get("say") == ["send it", "not now"], vb)
    check("approved by voice with heard and confidence", vb.get("approved") == {"by": "voice", "heard": "confirm", "confidence": 0.92, "tier": 1},
          vb.get("approved"))
    read_back(sid, card["draft_id"])
    r = say(sid, "send it")
    cd = voice_of(r).get("countdown") or {}
    check("send it arms a 5-second countdown, nothing sent", sends() == n_sent and cd.get("seconds") == 5 and cd.get("word") == "send it"
          and voice_of(r).get("spoken") == "Sending to Ravi in five seconds. Say stop to cancel."
          and voice_of(r).get("focus", {}).get("label") == "SENDING · SAY “STOP” TO CANCEL", voice_of(r))
    code, out = commit(sid, cd.get("arm_id"))
    check("commit before the deadline is held (409), nothing sent", code == 409 and out.get("error") == "held" and sends() == n_sent, (code, out))
    past_deadline()
    with voicestate.stt_running(sid):
        code, out = commit(sid, cd.get("arm_id"))
    check("commit while a transcription runs is held (409), nothing sent", code == 409 and out.get("error") == "held" and sends() == n_sent,
          (code, out))
    code, out = commit(sid, cd.get("arm_id"))
    via = [x for x in G.sent_raw[n_sent:] if x.get("draft_send")]
    check("commit at zero sends THAT saved draft once, via drafts.send, verified", code == 200 and sends() == n_sent + 1 and len(via) == 1
          and via[0]["draft_send"] == card["gmail_draft_id"] and out["receipt"]["status"] == "SENT" and out["receipt"]["verified"], (code, out.get("receipt")))
    check("only then does Vaani say sent", voice_of(out).get("spoken") == "Sent to Ravi, and Gmail confirmed it."
          and voice_of(out).get("focus", {}).get("label") == "SENT · VERIFIED", voice_of(out))
    code2, out2 = commit(sid, cd.get("arm_id"))
    check("a second commit sends nothing (410)", code2 == 410 and sends() == n_sent + 1, (code2, out2))
    rows = [x for x in tvp.rows(log) if x.get("tool") == "commit_pending" and x.get("card_kind")]
    check("Proof rows: confirmed_by voice, heard, stt_conf", len(rows) == 2 and all(x.get("confirmed_by") == "voice" for x in rows)
          and [x.get("heard") for x in rows] == ["confirm", "send it"] and all(x.get("stt_conf") == 0.92 for x in rows), rows)
    check("the streak counts voice approvals", autonomy.streak("replies") == 2, autonomy.streak("replies"))
    proof = CLIENT.get("/proof?workflow=replies").json()["rows"]
    check("GET /proof shows heard and stt_conf", proof and proof[0].get("heard") == "send it" and proof[0].get("stt_conf") == 0.92, proof[:1])

    d2 = reply_card(s)
    n_sent = sends()
    _, r, arm_id = armed(sid, s, card_fn=lambda _s: d2)
    past_deadline()
    code, out = commit(sid, arm_id)
    check("send it straight from the first email card: countdown, then one send", code == 200 and sends() == n_sent + 1
          and out["receipt"]["status"] == "SENT", (code, out.get("reply")))

    os.environ["VAANI_BLOCK_SEND"] = "1"
    try:
        n_sent = sends()
        card, r, arm_id = armed(sid, s)
        past_deadline()
        code, out = commit(sid, arm_id)
        check("VAANI_BLOCK_SEND=1 refuses the send; Vaani never says sent", code == 200 and sends() == n_sent
              and "switched off" in out.get("reply", "")
              and voice_of(out).get("spoken") == "Sending is switched off on this server, so nothing was sent."
              and s.pending_draft_id == card["draft_id"], (out.get("reply"), voice_of(out).get("spoken")))
    finally:
        os.environ.pop("VAANI_BLOCK_SEND", None)


def check_countdown_stops():
    sid, s = new_voice_session()
    for word in ("stop", "cancel", "no", "wait", "don't", "hold on"):
        n = sends()
        card, r, arm_id = armed(sid, s)
        r2 = say(sid, word, conf=0.2, overlap=True)  # stop words count even when unclear or overlapping
        past_deadline()
        code, out = commit(sid, arm_id)
        check(f"countdown: '{word}' stops it, nothing sent", sends() == n and code == 410 and out.get("error") == "stopped"
              and voice_of(r2).get("spoken") == "Stopped. Nothing was sent." and s.pending_draft_id == card["draft_id"], (word, voice_of(r2), code, out))

    n = sends()
    card, r, arm_id = armed(sid, s)
    r2 = say(sid, "next")
    check("countdown: any other word is ignored, the countdown keeps running",
          voice_of(r2).get("intent") == "ignored" and (voice_of(r2).get("countdown") or {}).get("arm_id") == arm_id, voice_of(r2))
    x = CLIENT.post("/voice/disarm", json={"session_id": sid, "reason": "esc"})
    past_deadline()
    code, out = commit(sid, arm_id)
    check("Esc / tap: /voice/disarm stops it, nothing sent", x.status_code == 200 and voice_of(x.json()).get("spoken") == "Stopped. Nothing was sent."
          and code == 410 and sends() == n, (x.json().get("voice"), code, out))
    x = CLIENT.post("/voice/disarm", json={"session_id": sid, "reason": "esc"})
    check("disarm with no countdown: ok, stopped false", x.json() == {"ok": True, "stopped": False}, x.json())

    card, r, arm_id = armed(sid, s)
    m = CLIENT.post("/voice/mode", json={"session_id": sid, "mode": "chat"}).json()
    past_deadline()
    code, out = commit(sid, arm_id)
    check("switching to chat mid-countdown stops it; commit refused", m == {"mode": "chat", "stopped": True} and code == 409 and sends() == n,
          (m, code, out))
    CLIENT.post("/voice/mode", json={"session_id": sid, "mode": "voice"})
    code, out = commit(sid, arm_id)
    check("and back in voice mode the old countdown stays stopped", code == 410 and out.get("error") == "stopped" and sends() == n, (code, out))

    card, r, arm_id = armed(sid, s)
    CLOCK.t += voicestate.COUNTDOWN_S + voicestate.COMMIT_GRACE_S + 1
    code, out = commit(sid, arm_id)
    check("commit after expiry: 410 expired, nothing sent", code == 410 and out.get("error") == "expired" and sends() == n, (code, out))

    card, r, arm_id = armed(sid, s)
    reply_card(s)  # the card changed under the countdown
    past_deadline()
    code, out = commit(sid, arm_id)
    check("commit when the card was replaced: refused, nothing sent", code == 410 and sends() == n, (code, out))
    code, out = commit(sid, "a-made-up")
    check("an unknown arm id: 410 not armed", code == 410 and out.get("error") == "not armed", (code, out))


def main() -> int:
    global G, C, CLIENT, REAL_CHAT
    import logging

    for name in ("assistant", "httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    os.environ["VAANI_AUTH"] = "single"
    tvp.fresh_log()
    G, C, _ = tm.setup()
    tvp.install_fakes()
    voicestate.reset_all()
    web._remember = fake_remember
    REAL_CHAT = flow.chat
    CLIENT = TestClient(web.app)
    for fn in (check_modes_and_typed, check_refusals, check_done_cards, check_email_flow, check_countdown_stops):
        try:
            fn()
        except Exception as e:  # a crash in one group is a failure, not the end of the run
            import traceback
            check(f"{fn.__name__} crashed", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
    flow.chat = REAL_CHAT
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} voiceturn checks passed")
    return 0 if passed == len(RESULTS) else 1


REAL_CHAT = flow.chat

if __name__ == "__main__":
    sys.exit(main())
