"""Voice-mode turn routes (POLICY): /voice/mode, /voice/turn, /voice/commit, /voice/disarm,
/voice/greeting. Contract: .build/CONTRACT-v3.md.

/voice/turn takes a transcript id, never text: the words come from voicestate, where only
/voice/stt and /voice/wake put them, so typed text or an email read aloud can never approve
anything. The fixed parser (voicecmd.parse) runs first; approvals are decided by
voice_policy.check; everything else goes to the fast paths or the model, exactly like a
chat turn (routes_stream.sse_turn: live steps, session lock, history row, backstop).
Every result is the /chat dict plus a "voice" block. Sign-in and X-CSRF-Token are enforced
by web.py's gate for every route here.
"""

from __future__ import annotations

import functools
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from assistant import agent, fastpath, flow, memory, obs, routes_stream, voice, voicecmd, voicestate
from assistant import voice_policy as vp

router = APIRouter()
log = logging.getLogger("assistant.routes_voiceturn")

FAST_INTENTS = ("brief", "next", "read", "calendar_today", "next_meeting")
GOODNIGHT = "Goodnight. Say Hey Vaani whenever you need me."
FALLBACK_GREETING = "Hello. Where shall we start?"


class ModeIn(BaseModel):
    session_id: str
    mode: str


class TurnIn(BaseModel):
    session_id: str
    transcript_id: str | None = None


class CommitIn(BaseModel):
    session_id: str
    arm_id: str


class DisarmIn(BaseModel):
    session_id: str
    reason: str | None = None


def _err(code: int, error: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": error, **extra})


def _voice_off() -> JSONResponse:
    return _err(409, "voice mode is off")


# --------------------------------------------------------------------------- /voice/mode, /voice/greeting


@router.post("/voice/mode")
async def voice_mode(request: Request, body: ModeIn):
    from assistant import web

    entry = web._entry_for(request, body.session_id)
    if entry is None:
        return web._unknown_session(body.session_id)
    if body.mode not in voicestate.MODES:
        return _err(422, "bad request: mode must be voice or chat")
    before = voicestate.current_arm(body.session_id)
    st = voicestate.set_mode(body.session_id, body.mode, web._uid(request))
    return {"mode": st.mode, "stopped": bool(before is not None and before.state == "stopped")}


def _first_name(user: dict | None) -> str:
    name = str((user or {}).get("name") or "").strip()
    if not name:
        try:
            name = str(memory.load().get("name") or "").strip()
        except Exception:
            name = ""
    return name.split()[0] if name.split() else ""


def _greeting(session, user: dict | None) -> str:
    try:
        return fastpath.greeting(session, _first_name(user)) or FALLBACK_GREETING
    except Exception:
        log.exception("greeting failed")
        return FALLBACK_GREETING


@router.get("/voice/greeting")
async def voice_greeting(request: Request, session_id: str):
    from assistant import web

    entry = web._entry_for(request, session_id)
    if entry is None:
        return web._unknown_session(session_id)
    user = web._user(request)
    return {"text": await web._call_as(user, _greeting, entry.session, user)}


def _waiting_card(session) -> dict:
    draft = agent.pending_draft(session)
    if not draft:
        return {"draft_id": None}
    return {"draft_id": draft["draft_id"], "focus": vp.focus_approval(draft), "readback": vp.readback_block(draft),
            "spoken": vp.readback_text(draft)}


@router.get("/voice/card")
async def voice_card(request: Request, session_id: str):
    """The card waiting for approval, from the server's own copy ({"draft_id": null} when none): what
    the page shows and reads out when it switches from Chat to Voice."""
    from assistant import web

    entry = web._entry_for(request, session_id)
    if entry is None:
        return web._unknown_session(session_id)
    return await web._call_as(web._user(request), _waiting_card, entry.session)


# --------------------------------------------------------------------------- /voice/turn


def _remember_spoken(session_id: str, out: dict) -> dict:
    vb = out.get("voice") or {}
    if vb.get("spoken"):
        voicestate.get(session_id).data["pol.last_spoken"] = vb["spoken"]
    return out


def _model_turn(session, session_id: str, t, cmd) -> dict:
    """The model (or the agent's own code paths and guards), exactly as a typed chat turn."""
    out = flow.chat(session, t.text)
    # The card sentence ("Tap Send when you're ready") is chat-mode wording: with_card adds the
    # read-back of a new card, or a short reminder for one already read back.
    spoken = voice.spoken_text({**out, "pending_draft": None})
    vb = vp.block(cmd.intent if cmd.intent != "none" else "chat", t, spoken=spoken, phase="listening")
    if out.get("receipt") and voice.receipt_ok(out["receipt"]):
        vb["phase"] = "done"
    return {**out, "voice": vp.with_card(vb, session)}


def _refused(session, t, verdict: vp.Verdict, intent: str, t0: float) -> dict:
    """A failed approval check: one fixed spoken reason, nothing written."""
    draft = agent.pending_draft(session)
    out = vp.code_out(session, t.text if t is not None else None, verdict.spoken, t0, append=t is not None)
    vb = vp.block(intent, t, spoken=verdict.spoken, phase="approval" if draft else "listening",
                  focus=vp.focus_approval(draft) if draft else None)
    if verdict.code == "readback" and draft:
        vb["readback"] = vp.readback_block(draft)
        vb["spoken"] = vp.readback_text(draft)
    return {**out, "voice": vb}


def _countdown_turn(session, session_id: str, t, cmd, a, t0: float) -> dict:
    """While a countdown runs only a stop word counts; anything else is ignored (it keeps running)."""
    if vp.is_stop_word(cmd, t.text) or cmd.intent == "switch_chat":
        stopped = voicestate.note_stop(session_id, "stop word")
        if stopped is not None:
            out = vp.stopped_result(session, session.drafts.get(stopped.draft_id), t.text, t0, "stop", t)
            if cmd.intent == "switch_chat":
                out["voice"]["client"] = "switch_chat"
            return out
        a = voicestate.current_arm(session_id)
        if a is None:  # it was committed in between: say what happened, never "stopped"
            return {**vp.code_out(session, None, "Too late to stop that one.", t0, append=False),
                    "voice": vp.block("stop", t, spoken="Too late to stop that one.", phase="listening")}
    left = round(a.remaining(), 1)
    out = vp.code_out(session, None, "Only stop counts during the countdown.", t0, append=False)
    vb = vp.block("ignored", t, spoken="", phase="countdown", focus=vp.focus_countdown(session.drafts.get(a.draft_id) or {}),
                  countdown={"arm_id": a.arm_id, "draft_id": a.draft_id, "seconds": left, "word": a.word})
    return {**out, "voice": vb}


def handle_turn(session, session_id: str, t) -> dict:
    """One voice turn (runs in the worker thread, session lock held). Never raises."""
    t0 = time.perf_counter()
    try:
        return _remember_spoken(session_id, _dispatch(session, session_id, t, t0))
    except Exception as e:
        log.exception("voice turn crashed")
        line = f"Something went wrong on my side ({type(e).__name__}). Nothing was changed; please try again."
        out = vp.code_out(session, None, line, t0, append=False)
        return {**out, "voice": vp.block("error", t, spoken="Something went wrong on my side. Nothing was changed.")}


def _dispatch(session, session_id: str, t, t0: float) -> dict:
    if t is None:  # unknown, used, expired, or another user's / session's transcript id
        line = vp.REASONS["stale"]
        draft = agent.pending_draft(session)
        out = vp.code_out(session, None, line, t0, append=False)
        return {**out, "voice": vp.block("stale", None, spoken=line, phase="approval" if draft else "listening",
                                         focus=vp.focus_approval(draft) if draft else None)}
    cmd = voicecmd.parse(t.text)
    intent = cmd.intent
    a = voicestate.current_arm(session_id)
    if a is not None:
        return _countdown_turn(session, session_id, t, cmd, a, t0)
    draft = agent.pending_draft(session)

    if intent == "stop":
        voicestate.note_stop(session_id, "stop word")
        out = vp.code_out(session, None, "Stopped.", t0, append=False)
        return {**out, "voice": vp.block("stop", t, spoken="", client="stop_speaking",
                                         phase="approval" if draft else "listening",
                                         focus=vp.focus_approval(draft) if draft else None)}
    if intent == "cancel":
        voicestate.note_stop(session_id, "cancel")
        if draft is None:
            out = vp.code_out(session, t.text, "There is nothing to cancel.", t0)
            return {**out, "voice": vp.block("cancel", t, spoken="There is nothing to cancel.")}
        out = flow.skip(session, draft["draft_id"])
        if (draft.get("kind") or "") == "email_send":
            line = "Okay, not now. The draft stays in Gmail." if draft.get("gmail_draft_id") else "Cancelled. Nothing was sent."
        else:
            line = "Cancelled. Nothing was changed."
        return {**out, "voice": vp.block("cancel", t, spoken=line, focus=vp.focus_stopped(draft), phase="listening")}
    if intent in ("confirm", "action"):
        if draft is None and not cmd.exact:
            return _model_turn(session, session_id, t, cmd)  # "confirm the time with Ravi": just words
        verdict = vp.check(session, session_id, t, cmd)
        if not verdict.ok:
            return _refused(session, t, verdict, intent, t0)
        if verdict.approval["tier"] == 1:
            return vp.commit_now(session, session_id, verdict, t)
        return vp.arm_countdown(session, session_id, verdict, t, t0)
    if intent == "undo":
        bad = vp.check_fresh(session_id, t, cmd)
        if bad is not None:
            return _refused(session, t, bad, intent, t0)
        did = vp.undo_target(session)
        if did is None:
            out = vp.code_out(session, t.text, "There is nothing to undo.", t0)
            return {**out, "voice": vp.block("undo", t, spoken="There is nothing to undo.")}
        v_tok = obs.current_voice.set({"heard": t.text, "confidence": t.confidence, "tid": t.tid})
        try:
            out = flow.action(session, "undo", draft_id=did)
        finally:
            obs.current_voice.reset(v_tok)
        rec = out.get("receipt")
        ok = voice.receipt_ok(rec)
        line = voice.speakable(str((rec or {}).get("report", [""])[0])) if ok else (voice.spoken_text(out) or "I could not undo that.")
        return {**out, "voice": vp.block("undo", t, spoken=f"Undone. {line}" if ok else line,
                                         focus=vp.focus_receipt(session.drafts.get(did) or {}, rec) if ok else None,
                                         phase="done" if ok else "listening")}
    if intent == "repeat":
        if draft is not None:
            return _refused(session, t, vp.Verdict(False, "readback", vp.readback_text(draft)), intent, t0)
        last = voicestate.get(session_id).data.get("pol.last_spoken") or ""
        out = vp.code_out(session, None, last or "There is nothing to repeat yet.", t0, append=False)
        return {**out, "voice": vp.block("repeat", t, spoken=last or "There is nothing to repeat yet.", client="repeat")}
    if intent in FAST_INTENTS:
        r = fastpath.handle(session, cmd, voicestate.get(session_id))
        if r is not None:
            vb = dict(r.get("voice") or vp.block(intent, t, spoken=voice.spoken_text(r)))
            vb["heard"] = t.text
            if vb.get("confidence") is None:
                vb["confidence"] = round(t.confidence, 2)
            return {**r, "voice": vp.with_card(vb, session)}
        return _model_turn(session, session_id, t, cmd)
    if intent == "screen_only":
        out = vp.code_out(session, t.text, vp.SCREEN_ONLY, t0)
        return {**out, "voice": vp.with_card(vp.block("screen_only", t, spoken=vp.SCREEN_ONLY), session)}
    if intent == "affirm" and draft is not None:
        word = vp.tier_of(draft)["word"] or "confirm"
        line = "Say confirm to go ahead." if word == "confirm" else f"Say {word} to go ahead."
        out = vp.code_out(session, t.text, line, t0)
        return {**out, "voice": vp.block("affirm", t, spoken=line, phase="approval", focus=vp.focus_approval(draft))}
    if intent == "goodnight":
        out = vp.code_out(session, t.text, GOODNIGHT, t0)
        return {**out, "voice": vp.block("goodnight", t, spoken=GOODNIGHT, phase="standby", client="goodnight")}
    if intent == "switch_chat":
        out = vp.code_out(session, t.text, "Switching to Chat.", t0)
        return {**out, "voice": vp.block("switch_chat", t, spoken="Switching to Chat.", client="switch_chat")}
    return _model_turn(session, session_id, t, cmd)  # none, change, affirm without a card


@router.post("/voice/turn")
async def voice_turn(request: Request, body: TurnIn):
    from assistant import web

    entry = web._entry_for(request, body.session_id)
    if entry is None:
        return web._unknown_session(body.session_id)
    sid = body.session_id
    if not voicestate.is_voice(sid):
        return _voice_off()
    t = voicestate.take_transcript(sid, web._uid(request), body.transcript_id or "")
    req = {"text": t.text if t else "", "via": "voice", "tid": body.transcript_id,
           "confidence": round(t.confidence, 2) if t else None}

    def setup():
        tok = obs.current_mode.set("voice")
        return lambda: obs.current_mode.reset(tok)

    call = functools.partial(handle_turn, entry.session, sid, t)
    return routes_stream.sse_turn(request, entry, sid, "chat", req, call, setup=setup)


# --------------------------------------------------------------------------- /voice/commit, /voice/disarm


def _commit_block(session, sid: str, arm_id: str):
    """The commit conditions (contract), checked with the session lock held. Returns the arm,
    or an error JSONResponse. Never claims the arm; the caller does, exactly once."""
    st = voicestate.peek(sid)
    a = st.arm if st else None
    if a is None or a.arm_id != str(arm_id):
        return _err(410, "not armed")
    if a.state == "armed":
        voicestate.current_arm(sid)  # marks it expired when the grace period is over
    if a.state == "stopped":
        return _err(410, "stopped")
    if a.state == "expired":
        return _err(410, "expired")
    if a.state != "armed":
        return _err(410, "not armed")
    if not voicestate.is_voice(sid):
        return _voice_off()
    at = voicestate.now()
    if at < a.deadline - voicestate.COMMIT_EARLY_S:
        return _err(409, "held", retry_ms=max(50, int((a.deadline - at) * 1000)))
    if st.stt_inflight > 0:
        return _err(409, "held", retry_ms=300)  # a "stop" may be in that recording
    draft = session.drafts.get(a.draft_id) or {}
    if session.pending_draft_id != a.draft_id or draft.get("status") != "DRAFT":
        voicestate.disarm(sid, "the card changed")
        return _err(410, "stopped")
    return a


@router.post("/voice/commit")
async def voice_commit(request: Request, body: CommitIn):
    from assistant import web

    entry = web._entry_for(request, body.session_id)
    if entry is None:
        return web._unknown_session(body.session_id)
    sid = body.session_id
    if not voicestate.is_voice(sid):
        return _voice_off()
    user = web._user(request)
    async with entry.lock:
        a = _commit_block(entry.session, sid, body.arm_id)
        if isinstance(a, JSONResponse):
            return a
        if not voicestate.mark_committed(sid, a.arm_id):  # exactly one commit per countdown
            return _err(410, "not armed")
        try:
            out = await web._call_as(user, vp.commit_armed, entry.session, sid, a)
        except Exception as e:  # never a 500
            log.exception("voice commit crashed")
            out = web._friendly(entry.session, f"Something went wrong on my side ({type(e).__name__}). Check Gmail or Google Calendar before retrying.")
            out["voice"] = vp.block("confirm", None, spoken="Something went wrong on my side. Check Google before trying again.")
    _remember_spoken(sid, out)
    await web._remember(request, sid, "confirm", {"draft_id": a.draft_id, "variant": a.variant, "via": "voice"}, out)
    return out


@router.post("/voice/disarm")
async def voice_disarm(request: Request, body: DisarmIn):
    from assistant import web

    entry = web._entry_for(request, body.session_id)
    if entry is None:
        return web._unknown_session(body.session_id)
    sid = body.session_id
    stopped = voicestate.note_stop(sid, body.reason or "tap") if voicestate.current_arm(sid) else None
    if stopped is None:
        return {"ok": True, "stopped": False}
    async with entry.lock:
        try:
            out = await web._call_as(web._user(request), vp.stopped_result, entry.session,
                                     entry.session.drafts.get(stopped.draft_id), None, time.perf_counter(), "stop")
        except Exception as e:
            log.exception("voice disarm crashed")
            out = web._friendly(entry.session, "Stopped. Nothing was sent.")
            out["voice"] = vp.block("stop", None, spoken="Stopped. Nothing was sent.", client="stop_speaking")
    _remember_spoken(sid, out)
    return out
