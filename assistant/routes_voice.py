"""Voice routes (see assistant/voice.py and .build/CONTRACT-v3.md, WAKE-STT):
POST /voice/stt, POST /voice/wake, POST /voice/tts, POST /voice/played, GET /voice/cue/{name},
GET /voice/status.

Sign-in and X-CSRF-Token are enforced by web.py's gate middleware for every route here.
Audio stays in memory: the request body goes straight to voice.transcribe().

With a session_id (voice mode) the server keeps what the approval rules need in
voicestate: each transcript gets a single-use id ("tid") with its confidence and capture
window, and every clip Vaani plays (read-back, reply, cue) is recorded with a speak id so
the page can report when it ended (/voice/played). Without one, /voice/stt and /voice/tts
work as they always did for the chat-mode mic and the speaker button.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from assistant import voice, voicestate

log = logging.getLogger("assistant.voice")

router = APIRouter()

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_WAKE_BYTES = 512 * 1024  # a wake segment is at most about 3 s
DEFAULT_CONF_MIN = 0.60


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": msg})


def _is_audio(content_type: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct.startswith("audio/") or ct == "application/octet-stream"


def _owned(request: Request, session_id: str):
    """(entry, None) for the caller's own chat session, else (None, the 404 response)."""
    from assistant import web  # lazy: web imports this module at the end of its own import

    entry = web._entry_for(request, session_id)
    return (entry, None) if entry is not None else (None, web._unknown_session(session_id))


def _uid(request: Request) -> str | None:
    from assistant import web

    return web._uid(request)


async def _read_audio(request: Request, cap: int):
    """The raw audio body as bytes, or an error response (415 not audio, 413 over cap, 503 not ready)."""
    if not _is_audio(request.headers.get("content-type", "")):
        return None, _err(415, "send the recording as audio/webm, audio/ogg, audio/mp4 or audio/wav")
    too_big = _err(413, "recording is over 2 MB; keep it under about a minute" if cap == MAX_AUDIO_BYTES
                   else "wake clip is over 512 KB; send at most about 3 s")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > cap:
        return None, too_big
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > cap:
            return None, too_big
    st = voice.status()
    if st["stt"] != "ready":
        if st["stt"] == "error":
            return None, _err(503, f"speech-to-text did not load: {st.get('error')}")
        voice.start_warmup()  # no-op while loading; starts it if nothing did yet
        return None, _err(503, "warming up")
    return bytes(data), None


async def _transcribe(data: bytes, purpose: str):
    """voice.transcribe in the threadpool -> (result, None) or (None, 503/415 response)."""
    try:
        return await run_in_threadpool(voice.transcribe, data, purpose), None
    except RuntimeError as e:  # unloaded between the check and the call
        return None, _err(503, str(e) or "warming up")
    except Exception as e:  # PyAV could not decode it: not audio after all
        log.warning("could not transcribe %d bytes: %s", len(data), type(e).__name__)
        return None, _err(415, "could not decode that audio")


def _empty(model: str | None) -> dict:
    return {"text": "", "ms": 0, "model": model, "empty": True, "confidence": 0.0, "duration_ms": 0}


@router.post("/voice/stt")
async def voice_stt(request: Request, session_id: str | None = None):
    """Raw audio body (audio/webm, audio/ogg, audio/mp4, audio/wav; 2 MB max) ->
    {"text", "ms", "model", "empty", "confidence", "duration_ms"}. With an owned session_id
    (voice mode) also {"overlap", "tid"}: the transcript is stored for one use within 15 s
    (tid null when nothing was heard); header "X-Vaani-Overlap: 1" = the page was playing Vaani."""
    arrived = voicestate.now()  # the clip ended when the page posted it
    if not voice.enabled():
        return _err(503, "voice is off")
    if session_id is not None:
        _entry, missing = _owned(request, session_id)
        if missing is not None:
            return missing
    data, problem = await _read_audio(request, MAX_AUDIO_BYTES)
    if problem is not None:
        return problem
    if session_id is None:  # chat-mode mic: nothing is kept
        if not data:
            return _empty(voice.status()["model"])
        res, problem = await _transcribe(data, "command")
        return problem or res
    client_overlap = request.headers.get("x-vaani-overlap", "").strip() == "1"
    with voicestate.stt_running(session_id):  # holds a countdown commit while a "stop" may be in this clip
        if data:
            res, problem = await _transcribe(data, "command")
            if problem is not None:
                return problem
        else:
            res = _empty(voice.status()["model"])
    capture_end = arrived
    capture_start = capture_end - res["duration_ms"] / 1000
    if res["empty"]:
        overlap = client_overlap or voicestate.speaking_overlaps(session_id, capture_start, capture_end)
        return dict(res, overlap=overlap, tid=None)
    t = voicestate.issue_transcript(session_id, _uid(request), res["text"], res["confidence"], capture_start,
                                    capture_end, client_overlap=client_overlap, purpose="command")
    return dict(res, confidence=t.confidence, overlap=t.overlap, tid=t.tid)


@router.post("/voice/wake")
async def voice_wake(request: Request, session_id: str):
    """A short standby clip (<= 512 KB, about 3 s) -> {"wake": false, "ms"} with no text when
    it does not start with "Hey Vaani" (nothing is kept or logged), else {"wake": true, "ms",
    "rest_text", "rest_tid", "confidence"}; rest_tid (purpose "wake_rest") only when words
    followed the name."""
    arrived = voicestate.now()
    if not voice.enabled():
        return _err(503, "voice is off")
    _entry, missing = _owned(request, session_id)
    if missing is not None:
        return missing
    data, problem = await _read_audio(request, MAX_WAKE_BYTES)
    if problem is not None:
        return problem
    if not data:
        return {"wake": False, "ms": 0}
    with voicestate.stt_running(session_id):
        res, problem = await _transcribe(data, "wake")
    if problem is not None:
        return problem
    matched, rest = voice.wake_match(res["text"])
    if not matched:
        return {"wake": False, "ms": res["ms"]}
    tid = None
    if rest:
        capture_start = arrived - res["duration_ms"] / 1000
        client_overlap = request.headers.get("x-vaani-overlap", "").strip() == "1"
        tid = voicestate.issue_transcript(session_id, _uid(request), rest, res["confidence"], capture_start, arrived,
                                          client_overlap=client_overlap, purpose="wake_rest").tid
    return {"wake": True, "ms": res["ms"], "rest_text": rest, "rest_tid": tid, "confidence": res["confidence"]}


class TtsIn(BaseModel):
    text: str | None = None
    result: dict | None = None
    voice: str | None = None
    session_id: str | None = None
    readback_draft_id: str | None = None


@router.post("/voice/tts")
async def voice_tts(request: Request, body: TtsIn):
    """{"text"?, "result"?, "voice"?, "session_id"?, "readback_draft_id"?} -> audio/wav, header
    X-Vaani-Spoken = the percent-encoded text spoken, X-Vaani-Audio-Ms = its length.
    With "result" (a /chat turn dict) the spoken text is built in code by voice.spoken_text().
    With readback_draft_id the text is the card's read-back, built from the server's own copy
    of the session's pending card (409 if it is not waiting); never the page's text.
    With session_id the clip is recorded (kind "readback" or "say") and X-Vaani-Speak-Id names it."""
    if not voice.enabled():
        return _err(503, "voice is off")
    kind, draft_id = "say", None
    if body.readback_draft_id is not None:
        if not body.session_id:
            return _err(400, "readback_draft_id needs session_id")
        entry, missing = _owned(request, body.session_id)
        if missing is not None:
            return missing
        session = entry.session
        draft = (session.drafts or {}).get(body.readback_draft_id)
        if not draft or session.pending_draft_id != body.readback_draft_id or draft.get("status") != "DRAFT":
            return _err(409, "that card is not waiting for approval")
        from assistant import voice_policy, web  # lazy: POLICY owns voice_policy

        try:
            text = await web._call_as(web._user(request), voice_policy.readback_text, draft)
        except Exception as e:
            log.exception("read-back text failed")
            return _err(503, f"could not build the read-back ({type(e).__name__})")
        text = " ".join(str(text or "").split())
        kind, draft_id = "readback", body.readback_draft_id
    else:
        if body.session_id:
            _entry, missing = _owned(request, body.session_id)
            if missing is not None:
                return missing
        if body.result is not None:
            text = voice.spoken_text(body.result)
        else:
            text = voice.speakable(body.text or "")
    text = text[: voice.MAX_TTS_CHARS].strip()
    if not text:
        if kind == "readback":
            return _err(409, "that card has nothing to read back")
        return Response(status_code=204)
    try:
        wav = await run_in_threadpool(voice.synth, text, body.voice)
    except Exception as e:
        log.error("text-to-speech failed: %s: %s", type(e).__name__, e)
        return _err(503, f"text-to-speech failed: {e}")
    audio_ms = voice.wav_ms(wav)
    headers = {"X-Vaani-Spoken": quote(text), "X-Vaani-Audio-Ms": str(audio_ms), "Cache-Control": "no-store"}
    if body.session_id:
        sp = voicestate.start_speech(body.session_id, kind, audio_ms, draft_id=draft_id, text=text)
        headers["X-Vaani-Speak-Id"] = sp.speak_id
    return Response(content=wav, media_type="audio/wav", headers=headers)


class PlayedIn(BaseModel):
    session_id: str
    speak_id: str
    complete: bool


@router.post("/voice/played")
async def voice_played(request: Request, body: PlayedIn):
    """{"session_id", "speak_id", "complete"} -> {"ok": true}: a clip finished playing
    (complete) or was cut off (barge-in, Esc, stop). The first report for a clip wins;
    {"ok": false} when the speak id is not this session's."""
    _entry, missing = _owned(request, body.session_id)
    if missing is not None:
        return missing
    sp = voicestate.end_speech(body.session_id, body.speak_id, body.complete)
    return {"ok": sp is not None}


@router.get("/voice/cue/{name}")
async def voice_cue(request: Request, name: str, session_id: str | None = None):
    """A pre-rendered cue (voice.CUES) as audio/wav: 404 for an unknown name, 503 until it is
    rendered. With session_id the clip is recorded (kind "cue") and X-Vaani-Speak-Id names it."""
    if name not in voice.CUES:
        return _err(404, f"unknown cue {name!r}")
    if not voice.enabled():
        return _err(503, "voice is off")
    if session_id is not None:
        _entry, missing = _owned(request, session_id)
        if missing is not None:
            return missing
    wav = voice.cue(name)
    if wav is None:
        voice.start_warmup()  # renders whatever is missing; no-op while it runs
        return _err(503, "cue is not ready yet")
    audio_ms = voice.wav_ms(wav)
    headers = {"X-Vaani-Spoken": quote(voice.CUES[name]), "X-Vaani-Audio-Ms": str(audio_ms), "Cache-Control": "no-store"}
    if session_id is not None:
        sp = voicestate.start_speech(session_id, "cue", audio_ms, text=voice.CUES[name])
        headers["X-Vaani-Speak-Id"] = sp.speak_id
    return Response(content=wav, media_type="audio/wav", headers=headers)


def _conf_min() -> float:
    try:
        from assistant import voice_policy

        fn = getattr(voice_policy, "conf_min", None)
        return float(fn()) if callable(fn) else DEFAULT_CONF_MIN
    except Exception:
        return DEFAULT_CONF_MIN


@router.get("/voice/status")
async def voice_status():
    """{"stt", "model", "error", "load_ms", "tts": "say", "voice", "voices": [{"name", "lang"}],
    "wake": "ready"|"off", "conf_min", "cues": [rendered cue names]}."""
    out = voice.status()
    found = await run_in_threadpool(voice.voices)
    out.update({"tts": "say" if found else "off", "voice": voice.resolve_voice(None) or voice.tts_voice(),
                "voices": found, "wake": "ready" if out["stt"] == "ready" else "off", "conf_min": _conf_min(),
                "cues": voice.cue_names()})
    return out
