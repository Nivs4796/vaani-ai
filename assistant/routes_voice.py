"""Voice routes: POST /voice/stt, POST /voice/tts, GET /voice/status (see assistant/voice.py).

Sign-in and X-CSRF-Token are enforced by web.py's gate middleware for every route here.
Audio stays in memory: the request body goes straight to voice.transcribe().
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from assistant import voice

log = logging.getLogger("assistant.voice")

router = APIRouter()

MAX_AUDIO_BYTES = 2 * 1024 * 1024


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": msg})


def _is_audio(content_type: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct.startswith("audio/") or ct == "application/octet-stream"


@router.post("/voice/stt")
async def voice_stt(request: Request):
    """Raw audio body (audio/webm, audio/ogg, audio/mp4, audio/wav; 2 MB max) -> {"text", "ms", "model", "empty"}."""
    if not voice.enabled():
        return _err(503, "voice is off")
    if not _is_audio(request.headers.get("content-type", "")):
        return _err(415, "send the recording as audio/webm, audio/ogg, audio/mp4 or audio/wav")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_AUDIO_BYTES:
        return _err(413, "recording is over 2 MB; keep it under about a minute")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_AUDIO_BYTES:
            return _err(413, "recording is over 2 MB; keep it under about a minute")
    st = voice.status()
    if st["stt"] != "ready":
        if st["stt"] == "error":
            return _err(503, f"speech-to-text did not load: {st.get('error')}")
        voice.start_warmup()  # no-op while loading; starts it if nothing did yet
        return _err(503, "warming up")
    if not data:
        return {"text": "", "ms": 0, "model": st["model"], "empty": True}
    try:
        return await run_in_threadpool(voice.transcribe, bytes(data))
    except RuntimeError as e:  # unloaded between the check and the call
        return _err(503, str(e) or "warming up")
    except Exception as e:  # PyAV could not decode it: not audio after all
        log.warning("could not transcribe %d bytes: %s: %s", len(data), type(e).__name__, e)
        return _err(415, "could not decode that audio")


class TtsIn(BaseModel):
    text: str | None = None
    result: dict | None = None
    voice: str | None = None


@router.post("/voice/tts")
async def voice_tts(body: TtsIn):
    """{"text"?, "result"?, "voice"?} -> audio/wav, header X-Vaani-Spoken = the percent-encoded text spoken.
    With "result" (a /chat turn dict) the spoken text is built in code by voice.spoken_text()."""
    if not voice.enabled():
        return _err(503, "voice is off")
    if body.result is not None:
        text = voice.spoken_text(body.result)
    else:
        text = voice.speakable(body.text or "")
    text = text[: voice.MAX_TTS_CHARS].strip()
    if not text:
        return Response(status_code=204)
    try:
        wav = await run_in_threadpool(voice.synth, text, body.voice)
    except Exception as e:
        log.error("text-to-speech failed: %s: %s", type(e).__name__, e)
        return _err(503, f"text-to-speech failed: {e}")
    return Response(content=wav, media_type="audio/wav",
                    headers={"X-Vaani-Spoken": quote(text), "Cache-Control": "no-store"})


@router.get("/voice/status")
async def voice_status():
    """{"stt", "model", "error", "load_ms", "tts": "say", "voice", "voices": [{"name", "lang"}]}."""
    out = voice.status()
    found = await run_in_threadpool(voice.voices)
    out.update({"tts": "say" if found else "off", "voice": voice.resolve_voice(None) or voice.tts_voice(),
                "voices": found})
    return out
