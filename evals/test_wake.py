"""Offline checks for the voice-first wake and STT layer (WAKE-STT, .build/CONTRACT-v3.md):
voice.wake_match() on a table of what Whisper writes (and ordinary speech it must ignore),
the session routes /voice/stt?session_id=, /voice/wake, /voice/tts (read-backs and speak
ids), /voice/played and /voice/cue/{name} with a patched transcribe(), and, when the
small.en model is in the local cache, a real round trip on the static/test clips that
prints the measured confidences and the wake latency.

No network: the model loads with local_files_only=True and HF_HUB_OFFLINE=1. Made-up data only.

Run: .venv/bin/python -m evals.test_wake   (exit 1 on any failure)
"""

from __future__ import annotations

import os
import re
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import unquote

# Set before any assistant import (python-dotenv would fill unset ones from .env).
os.environ.update({"VAANI_AUTH": "single", "VAANI_VOICE": "1", "VAANI_STT_MODEL": "small.en", "VAANI_STT_COMPUTE": "int8",
                   "VAANI_TTS_VOICE": "Samantha", "VAANI_TTS_RATE": "185", "HF_HUB_OFFLINE": "1"})

from fastapi.testclient import TestClient  # noqa: E402

from assistant import agent, voice, voice_policy, voicestate, web  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "static" / "test"
SID = "wk000001"

CHECKS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


def words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


# (what Whisper wrote, wake?, rest)
WAKE_TABLE: list[tuple[str, bool, str]] = [
    # the name as Whisper spells it
    ("Hey Vaani.", True, ""),
    ("Hey Vaani", True, ""),
    ("hey vaani", True, ""),
    ("Hey Vani.", True, ""),
    ("Hey Varni.", True, ""),
    ("Hey Bani!", True, ""),
    ("Hey Vaney.", True, ""),
    ("Hey Vahni.", True, ""),
    ("Hey Vanny.", True, ""),
    ("Hey Vanni?", True, ""),
    ("Hey Varney.", True, ""),
    ("Hi Vaani.", True, ""),
    ("Hi, Vaani!", True, ""),
    ("A Vaani.", True, ""),
    ("Hey, Vaani.", True, ""),
    ("Hey-Vaani.", True, ""),
    ("Haevani", True, ""),
    ("Heyvaani.", True, ""),
    ("Avani.", True, ""),
    ("Hello Vaani.", True, ""),
    ("Hey Va ni.", True, ""),
    ("Vaani.", True, ""),
    ("Um, hey Vaani.", True, ""),
    ("Oh hey Vani.", True, ""),
    ("Okay, hey Vaani.", True, ""),
    # with a command after it
    ("Hey Vaani, brief me.", True, "brief me"),
    ("Hey Vani, breathe me.", True, "breathe me"),
    ("Hey Vaani, what's on my calendar today?", True, "what's on my calendar today"),
    ("Hi Vaani read it", True, "read it"),
    ("Hey, Varni, next.", True, "next"),
    ("Hey Bani stop!", True, "stop"),
    ("Hey Vaani... reply saying I'll be late.", True, "reply saying I'll be late"),
    ("A Vaani, confirm.", True, "confirm"),
    ("Vaani, read it.", True, "read it"),
    ("Haevani, what's next?", True, "what's next"),
    ("Hey Vaani — send it!", True, "send it"),
    ("Hey Vaani, \"Ravi Mehta\" first.", True, "Ravi Mehta\" first"),
    # ordinary speech: never a wake
    ("Have any of you seen the weather today?", False, ""),
    ("Hey you.", False, ""),
    ("Hey funny.", False, ""),
    ("Every one of them.", False, ""),
    ("Hello there.", False, ""),
    ("Hey.", False, ""),
    ("Hey!", False, ""),
    ("", False, ""),
    ("   ", False, ""),
    ("...", False, ""),
    ("Hey bunny.", False, ""),
    ("Hey Benny, are you coming?", False, ""),
    ("Hey honey.", False, ""),
    ("Hey money.", False, ""),
    ("Hey Siri.", False, ""),
    ("Hey Vinny.", False, ""),
    ("Hey Bonnie.", False, ""),
    ("Hey Danny, read it.", False, ""),
    ("Hey Johnny.", False, ""),
    ("Hey there, Vaani.", False, ""),  # the name must be in the first words straight after the greeting
    ("Hey any updates?", False, ""),
    ("Hey many of us are late.", False, ""),
    ("Heavy rain today.", False, ""),
    ("Advani said hello.", False, ""),
    ("Havana is lovely.", False, ""),
    ("Vanilla or chocolate?", False, ""),
    ("Hey Vanessa.", False, ""),
    ("Any of you free?", False, ""),
    ("I told Vaani yesterday.", False, ""),
    ("So what did Vaani say?", False, ""),
    ("Confirm.", False, ""),
    ("Send it.", False, ""),
    ("Thank you.", False, ""),
    ("Hey hey.", False, ""),
]


def wake_checks() -> int:
    n0 = len(CHECKS)
    for text, want, rest in WAKE_TABLE:
        got = voice.wake_match(text)
        ok = got[0] is want and (got[1] == rest if want else got == (False, ""))
        check(f"wake_match({text!r}) -> {want}, {rest!r}", ok, got)
    return len(CHECKS) - n0


# --------------------------------------------------------------------------- routes with a patched transcribe

class FakeSTT:
    """transcribe(data, purpose) for the route tests: the body is "text|confidence|duration_ms"."""

    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, data: bytes, purpose: str = "command") -> dict:
        text, conf, dur = (data.decode("utf-8").split("|") + ["0.9", "1200"])[:3]
        st = voicestate.peek(SID)
        self.calls.append((text, purpose, st.stt_inflight if st else -1))
        return {"text": text, "ms": 7, "model": "fake", "empty": not text, "confidence": float(conf),
                "duration_ms": int(dur)}


def audio(text: str, conf: float = 0.9, dur: int = 1200) -> bytes:
    return f"{text}|{conf}|{dur}".encode()


def new_session(sid: str = SID) -> agent.Session:
    session = agent.Session(id=sid, messages=[{"role": "system", "content": "test"}])
    web.SESSIONS[sid] = web._Entry(session=session, owner=None)
    return session


def route_checks(c: TestClient) -> None:
    real = {"status": voice.status, "transcribe": voice.transcribe, "readback": voice_policy.readback_text,
            "now": voicestate.now, "cues": dict(voice._cues)}
    fake = FakeSTT()
    voice.status = lambda: {"stt": "ready", "model": "fake", "error": None, "load_ms": 1}
    voice.transcribe = fake
    voicestate.reset_all()
    session = new_session()
    wav_ct = {"Content-Type": "audio/wav"}
    try:
        # ---- /voice/stt without a session: the chat-mode mic, nothing kept
        r = c.post("/voice/stt", content=audio("What's on my calendar today?"), headers=wav_ct)
        body = r.json()
        check("stt without session: today's keys plus confidence and duration_ms",
              r.status_code == 200 and set(body) == {"text", "ms", "model", "empty", "confidence", "duration_ms"}, body)
        check("stt without session keeps nothing", voicestate.peek(SID) is None, voicestate.peek(SID))
        check("stt without session ran the command purpose", fake.calls[-1][1] == "command", fake.calls)

        # ---- with a session
        r = c.post("/voice/stt?session_id=nope", content=audio("next"), headers=wav_ct)
        check("stt with an unknown session is 404", r.status_code == 404, (r.status_code, r.text))
        t_before = voicestate.now()
        r = c.post(f"/voice/stt?session_id={SID}", content=audio("Confirm.", 0.83, 900), headers=wav_ct)
        body = r.json()
        check("stt with session: the contract's keys",
              r.status_code == 200 and set(body) == {"text", "ms", "model", "empty", "confidence", "overlap", "duration_ms", "tid"}, body)
        check("stt with session issues a tid", bool(body.get("tid")) and body["text"] == "Confirm." and body["overlap"] is False
              and body["confidence"] == 0.83, body)
        check("stt runs inside stt_running", fake.calls[-1] == ("Confirm.", "command", 1), fake.calls[-1])
        check("stt_inflight back to 0", voicestate.get(SID).stt_inflight == 0)
        t = voicestate.take_transcript(SID, None, body["tid"])
        check("take_transcript hands it out", t is not None and t.text == "Confirm." and t.purpose == "command"
              and abs(t.confidence - 0.83) < 1e-9 and not t.overlap, t)
        check("capture window = arrival minus the decoded duration",
              t is not None and abs((t.capture_end - t.capture_start) - 0.9) < 1e-6 and t_before <= t.capture_end <= voicestate.now(), t)
        check("a tid is single use", voicestate.take_transcript(SID, None, body["tid"]) is None)
        tid = c.post(f"/voice/stt?session_id={SID}", content=audio("next"), headers=wav_ct).json()["tid"]
        check("a tid is not another user's", voicestate.take_transcript(SID, "someone-else", tid) is None)
        tid = c.post(f"/voice/stt?session_id={SID}", content=audio("next"), headers=wav_ct).json()["tid"]
        check("a tid is not another session's", voicestate.take_transcript("other-session", None, tid) is None)
        voicestate.now = lambda: real["now"]() + voicestate.TRANSCRIPT_TTL_S + 1
        check("a tid expires after 15 s", voicestate.take_transcript(SID, None, tid) is None)
        voicestate.now = real["now"]
        r = c.post(f"/voice/stt?session_id={SID}", content=audio(""), headers=wav_ct)
        body = r.json()
        check("nothing heard: no tid", r.status_code == 200 and body["empty"] is True and body["tid"] is None, body)
        r = c.post(f"/voice/stt?session_id={SID}", content=b"", headers=wav_ct)
        check("empty body: no tid", r.status_code == 200 and r.json().get("tid") is None and r.json().get("empty") is True, r.text)
        r = c.post(f"/voice/stt?session_id={SID}", content=audio("stop"), headers={**wav_ct, "X-Vaani-Overlap": "1"})
        body = r.json()
        t = voicestate.take_transcript(SID, None, body["tid"])
        check("X-Vaani-Overlap: 1 marks the transcript", body["overlap"] is True and t is not None and t.overlap is True, body)

        # ---- a capture overlapping Vaani's own speech
        r = c.post("/voice/tts", json={"text": "Ravi Mehta wants the quote by Friday.", "session_id": SID})
        speak_id = r.headers.get("x-vaani-speak-id", "")
        audio_ms = int(r.headers.get("x-vaani-audio-ms") or 0)
        check("tts with session: speak id and audio length", r.status_code == 200 and speak_id.startswith("s") and audio_ms > 500
              and r.content[:4] == b"RIFF" and audio_ms == voice.wav_ms(r.content), dict(r.headers))
        sp = next((s for s in voicestate.get(SID).speeches if s.speak_id == speak_id), None)
        check("tts with session records a 'say' clip", sp is not None and sp.kind == "say" and sp.audio_ms == audio_ms
              and sp.text == "Ravi Mehta wants the quote by Friday.", sp)
        r = c.post(f"/voice/stt?session_id={SID}", content=audio("confirm", 0.9, 1500), headers=wav_ct)
        body = r.json()
        t = voicestate.take_transcript(SID, None, body["tid"])
        check("a capture while Vaani speaks overlaps (no header needed)", body["overlap"] is True and t is not None and t.overlap, body)
        r = c.post("/voice/played", json={"session_id": SID, "speak_id": speak_id, "complete": True})
        check("played -> ok", r.status_code == 200 and r.json() == {"ok": True}, r.text)
        check("played marks the clip complete", sp.complete is True and sp.ended_at is not None, sp)
        r = c.post("/voice/played", json={"session_id": SID, "speak_id": speak_id, "complete": False})
        check("the first played report wins", r.json() == {"ok": True} and sp.complete is True, sp)
        check("played with an unknown speak id -> ok false",
              c.post("/voice/played", json={"session_id": SID, "speak_id": "s000000000000", "complete": True}).json() == {"ok": False})
        check("played for an unknown session is 404",
              c.post("/voice/played", json={"session_id": "nope", "speak_id": speak_id, "complete": True}).status_code == 404)
        later = real["now"]() + 5
        voicestate.now = lambda: later
        r = c.post(f"/voice/stt?session_id={SID}", content=audio("confirm", 0.9, 1500), headers=wav_ct)
        body = r.json()
        check("a capture after the clip ended does not overlap", body["overlap"] is False, body)
        voicestate.now = real["now"]

        # ---- /voice/wake
        n_before = len(voicestate.get(SID).transcripts)
        r = c.post(f"/voice/wake?session_id={SID}", content=audio("Have any of you seen the weather today?"), headers=wav_ct)
        body = r.json()
        check("no wake word: only {wake: false, ms}", r.status_code == 200 and body == {"wake": False, "ms": 7}, body)
        check("no wake word: no text anywhere in the response", "weather" not in r.text, r.text)
        check("no wake word: nothing kept", len(voicestate.get(SID).transcripts) == n_before)
        check("wake runs the wake purpose inside stt_running", fake.calls[-1][1:] == ("wake", 1), fake.calls[-1])
        r = c.post(f"/voice/wake?session_id={SID}", content=audio("Hey Vaani, brief me.", 0.88, 1600), headers=wav_ct)
        body = r.json()
        check("wake with a command", r.status_code == 200 and body.get("wake") is True and body.get("rest_text") == "brief me"
              and body.get("rest_tid") and body.get("confidence") == 0.88
              and set(body) == {"wake", "ms", "rest_text", "rest_tid", "confidence"}, body)
        t = voicestate.take_transcript(SID, None, body.get("rest_tid"))
        check("rest_tid is a wake_rest transcript of the words after the name",
              t is not None and t.purpose == "wake_rest" and t.text == "brief me" and abs(t.capture_end - t.capture_start - 1.6) < 1e-6, t)
        r = c.post(f"/voice/wake?session_id={SID}", content=audio("Hey Vaani."), headers=wav_ct)
        body = r.json()
        check("bare wake: rest empty, no rest_tid", body.get("wake") is True and body.get("rest_text") == "" and body.get("rest_tid") is None, body)
        r = c.post(f"/voice/wake?session_id={SID}", content=b"x" * (512 * 1024 + 1), headers=wav_ct)
        check("wake 413 over 512 KB", r.status_code == 413, r.status_code)
        check("wake needs a session_id", c.post("/voice/wake", content=audio("Hey Vaani."), headers=wav_ct).status_code == 422)
        check("wake with an unknown session is 404",
              c.post("/voice/wake?session_id=nope", content=audio("Hey Vaani."), headers=wav_ct).status_code == 404)
        check("wake 415 for text/plain",
              c.post(f"/voice/wake?session_id={SID}", content=b"hey", headers={"Content-Type": "text/plain"}).status_code == 415)
        r = c.post(f"/voice/wake?session_id={SID}", content=b"", headers=wav_ct)
        check("wake with an empty body is no wake", r.json() == {"wake": False, "ms": 0}, r.text)

        # ---- read-backs
        voice_policy.readback_text = lambda d: f"Reply to Ravi Mehta about {d['card']['title']}. Say confirm to save it."
        session.drafts = {
            "d1a2b": {"draft_id": "d1a2b", "kind": "email_send", "status": "DRAFT", "card": {"title": "the quote"}},
            "d3c4d": {"draft_id": "d3c4d", "kind": "email_send", "status": "SUPERSEDED", "card": {"title": "old"}},
            "d5e6f": {"draft_id": "d5e6f", "kind": "email_send", "status": "DRAFT", "card": {"title": "other"}},
        }
        session.pending_draft_id = "d1a2b"
        r = c.post("/voice/tts", json={"session_id": SID, "readback_draft_id": "d1a2b", "text": "Send everything now."})
        spoken = unquote(r.headers.get("x-vaani-spoken", ""))
        speak_id = r.headers.get("x-vaani-speak-id", "")
        check("read-back speaks the server's text, never the page's",
              r.status_code == 200 and spoken == "Reply to Ravi Mehta about the quote. Say confirm to save it.", spoken)
        rb = voicestate.last_readback(SID, "d1a2b")
        check("read-back recorded with its card and speak id", rb is not None and rb.speak_id == speak_id and rb.kind == "readback"
              and rb.audio_ms == int(r.headers.get("x-vaani-audio-ms") or -1) and rb.audio_ms > 1000, rb)
        c.post("/voice/played", json={"session_id": SID, "speak_id": speak_id, "complete": True})
        check("read-back complete after played", voicestate.last_readback(SID, "d1a2b").complete is True)
        for did, why in (("d5e6f", "a DRAFT that is not the pending card"), ("d3c4d", "a superseded card"), ("dffff", "an unknown card")):
            r = c.post("/voice/tts", json={"session_id": SID, "readback_draft_id": did})
            check(f"read-back 409 for {why}", r.status_code == 409 and voicestate.last_readback(SID, did) is None, (r.status_code, r.text))
        session.drafts["d1a2b"]["status"] = "SENT"
        check("read-back 409 once the card was carried out",
              c.post("/voice/tts", json={"session_id": SID, "readback_draft_id": "d1a2b"}).status_code == 409)
        check("read-back without a session is 400", c.post("/voice/tts", json={"readback_draft_id": "d1a2b"}).status_code == 400)
        check("read-back for an unknown session is 404",
              c.post("/voice/tts", json={"session_id": "nope", "readback_draft_id": "d1a2b"}).status_code == 404)
        r = c.post("/voice/tts", json={"text": "Hello."})
        check("tts without a session: no speak id, still the audio length",
              r.status_code == 200 and "x-vaani-speak-id" not in r.headers and int(r.headers.get("x-vaani-audio-ms") or 0) > 0, dict(r.headers))

        # ---- cues
        voice._cues.clear()
        r = c.get("/voice/cue/no_such_cue")
        check("cue: unknown name is 404", r.status_code == 404, r.status_code)
        voice._cue_state["started"] = True  # keep the route's retry from rendering in the background here
        r = c.get("/voice/cue/on_it")
        check("cue: 503 while not rendered", r.status_code == 503, r.status_code)
        wav = voice.synth("On it.")
        voice._cues["on_it"] = wav
        r = c.get("/voice/cue/on_it")
        check("cue served from memory", r.status_code == 200 and r.content == wav and "x-vaani-speak-id" not in r.headers
              and int(r.headers.get("x-vaani-audio-ms") or 0) == voice.wav_ms(wav), dict(r.headers))
        r = c.get(f"/voice/cue/on_it?session_id={SID}")
        sp = next((s for s in voicestate.get(SID).speeches if s.speak_id == r.headers.get("x-vaani-speak-id")), None)
        check("cue with a session is recorded as kind cue", sp is not None and sp.kind == "cue" and sp.text == "On it.", sp)
        check("cue for an unknown session is 404", c.get("/voice/cue/on_it?session_id=nope").status_code == 404)
        st = c.get("/voice/status").json()
        check("status: wake, conf_min, cues", st.get("wake") == "ready" and isinstance(st.get("conf_min"), float)
              and 0 < st["conf_min"] <= 1 and st.get("cues") == ["on_it"], {k: st.get(k) for k in ("wake", "conf_min", "cues")})

        os.environ["VAANI_VOICE"] = "0"
        check("wake 503 when voice is off", c.post(f"/voice/wake?session_id={SID}", content=audio("Hey Vaani."), headers=wav_ct).status_code == 503)
        check("cue 503 when voice is off", c.get("/voice/cue/on_it").status_code == 503)
        os.environ["VAANI_VOICE"] = "1"
    finally:
        os.environ["VAANI_VOICE"] = "1"
        voice.status, voice.transcribe, voice_policy.readback_text, voicestate.now = (
            real["status"], real["transcribe"], real["readback"], real["now"])
        voice._cues.clear()
        voice._cues.update(real["cues"])
        voice._cue_state["started"] = False
        web.SESSIONS.pop(SID, None)
        voicestate.reset_all()


def google_checks() -> None:
    """google mode: the gate's 401 and CSRF 403 cover the new routes, sessions are per user
    and a transcript belongs to the signed-in user who spoke it."""
    import base64
    import json

    import itsdangerous

    real = {"status": voice.status, "transcribe": voice.transcribe, "auth": os.environ.get("VAANI_AUTH")}
    voice.status = lambda: {"stt": "ready", "model": "fake", "error": None, "load_ms": 1}
    voice.transcribe = FakeSTT()
    os.environ["VAANI_AUTH"] = "google"
    voicestate.reset_all()
    wav_ct = {"Content-Type": "audio/wav"}
    try:
        anon = TestClient(web.app)
        check("google: signed out, /voice/wake is 401",
              anon.post(f"/voice/wake?session_id={SID}", content=audio("Hey Vaani."), headers=wav_ct).status_code == 401)
        check("google: signed out, /voice/cue is 401", anon.get("/voice/cue/on_it").status_code == 401)
        signer = itsdangerous.TimestampSigner(str(web._session_secret))
        payload = {"user": {"id": "u-alex", "email": "alex@example.org", "name": "Alex"}, "csrf": "tok-alex"}
        cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
        c = TestClient(web.app, cookies={web.SESSION_COOKIE: cookie})
        new_session()
        web.SESSIONS[SID].owner = "u-alex"
        csrf = {**wav_ct, "X-CSRF-Token": "tok-alex"}
        r = c.post(f"/voice/stt?session_id={SID}", content=audio("next"), headers=wav_ct)
        check("google: /voice/stt without X-CSRF-Token is 403", r.status_code == 403, r.status_code)
        check("google: /voice/played without X-CSRF-Token is 403",
              c.post("/voice/played", json={"session_id": SID, "speak_id": "s1", "complete": True}).status_code == 403)
        r = c.post(f"/voice/stt?session_id={SID}", content=audio("next"), headers=csrf)
        tid = r.json().get("tid")
        check("google: with the header the owner gets a tid", r.status_code == 200 and bool(tid), r.text)
        check("google: the transcript is the signed-in user's", voicestate.take_transcript(SID, None, tid) is None)
        tid = c.post(f"/voice/stt?session_id={SID}", content=audio("next"), headers=csrf).json().get("tid")
        t = voicestate.take_transcript(SID, "u-alex", tid)
        check("google: ... and only theirs", t is not None and t.user_id == "u-alex", t)
        r = c.post(f"/voice/wake?session_id={SID}", content=audio("Hey Vaani, next."), headers=csrf)
        t = voicestate.take_transcript(SID, "u-alex", r.json().get("rest_tid"))
        check("google: the wake rest is the signed-in user's", t is not None and t.purpose == "wake_rest" and t.text == "next", r.text)
        web.SESSIONS[SID].owner = "u-other"
        check("google: another user's session looks unknown (stt)",
              c.post(f"/voice/stt?session_id={SID}", content=audio("next"), headers=csrf).status_code == 404)
        check("google: another user's session looks unknown (wake)",
              c.post(f"/voice/wake?session_id={SID}", content=audio("Hey Vaani."), headers=csrf).status_code == 404)
        check("google: another user's session looks unknown (tts, played, cue)",
              c.post("/voice/tts", json={"text": "Hi.", "session_id": SID}, headers={"X-CSRF-Token": "tok-alex"}).status_code == 404
              and c.post("/voice/played", json={"session_id": SID, "speak_id": "s1", "complete": True},
                         headers={"X-CSRF-Token": "tok-alex"}).status_code == 404
              and c.get(f"/voice/cue/on_it?session_id={SID}").status_code == 404)
    finally:
        voice.status, voice.transcribe = real["status"], real["transcribe"]
        os.environ["VAANI_AUTH"] = real["auth"] or "single"
        web.SESSIONS.pop(SID, None)
        voicestate.reset_all()


# --------------------------------------------------------------------------- the real model

def model_checks(c: TestClient) -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError as e:
        print(f"SKIPPED  real wake round trip: faster-whisper is not installed ({e})")
        return
    voice.start_warmup()
    t0 = time.perf_counter()
    while voice.status()["stt"] == "loading" and time.perf_counter() - t0 < 180:
        time.sleep(0.1)
    st = voice.status()
    if st["stt"] != "ready":
        print(f"SKIPPED  real wake round trip: speech model not ready ({st.get('error')})")
        return
    new_session()
    wav_ct = {"Content-Type": "audio/wav"}
    try:
        def wake(fname):
            t1 = time.perf_counter()
            r = c.post(f"/voice/wake?session_id={SID}", content=(FIXTURES / fname).read_bytes(), headers=wav_ct)
            return r, int((time.perf_counter() - t1) * 1000)

        r, ms = wake("voice-wake-brief.wav")
        body = r.json()
        rest = words(body.get("rest_text", ""))
        check("real: 'Hey Vaani, brief me.' wakes with rest ~ 'brief me'",
              r.status_code == 200 and body.get("wake") is True and "me" in rest and len(rest) <= 3 and body.get("rest_tid"), body)
        print(f"wake voice-wake-brief.wav: {body} (route {ms} ms)")
        r, ms = wake("voice-wake.wav")
        body = r.json()
        check("real: 'Hey Vaani.' wakes with an empty rest", body.get("wake") is True and body.get("rest_text") == ""
              and body.get("rest_tid") is None, body)
        print(f"wake voice-wake.wav: {body} (route {ms} ms)")
        for fname in ("voice-not-wake.wav", "voice-calendar.wav", "voice-confirm.wav"):
            r, ms = wake(fname)
            check(f"real: {fname} is not a wake, no text returned", r.json().get("wake") is False and set(r.json()) == {"wake", "ms"}, r.text)
            print(f"wake {fname}: {r.json()} (route {ms} ms)")
        data = (FIXTURES / "voice-wake.wav").read_bytes()
        runs = [voice.transcribe(data, "wake")["ms"] for _ in range(3)]
        print(f"wake latency (voice-wake.wav, transcribe purpose wake): runs {runs} ms, median {statistics.median(runs):.0f} ms")
        check("wake transcription under 2 s on this Mac", statistics.median(runs) < 2000, runs)

        print("command confidences (purpose command; set VAANI_VOICE_CONF_MIN from these):")
        for fname, want in (("voice-confirm.wav", "confirm"), ("voice-send-it.wav", "send it"), ("voice-stop.wav", "stop"),
                            ("voice-next.wav", "next"), ("voice-read-it.wav", "read it"),
                            ("voice-calendar.wav", "what's on my calendar today")):
            res = voice.transcribe((FIXTURES / fname).read_bytes())
            check(f"real: {fname} heard as {want!r} with a confidence in (0, 1]",
                  words(res["text"]) == words(want) and 0 < res["confidence"] <= 1 and res["duration_ms"] > 300, res)
            print(f"  {fname:20} conf {res['confidence']:.3f}  {res['ms']:4d} ms  {res['duration_ms']:5d} ms audio  -> {res['text']!r}")
        r = c.post(f"/voice/stt?session_id={SID}", content=(FIXTURES / "voice-send-it.wav").read_bytes(), headers=wav_ct)
        body = r.json()
        t = voicestate.take_transcript(SID, None, body.get("tid"))
        check("real: /voice/stt with a session issues a tid for 'Send it.'",
              t is not None and words(t.text) == ["send", "it"] and t.confidence == body["confidence"]
              and abs((t.capture_end - t.capture_start) * 1000 - body["duration_ms"]) < 2, body)
    finally:
        web.SESSIONS.pop(SID, None)
        voicestate.reset_all()


def main() -> int:
    c = TestClient(web.app)  # no `with`: the lifespan (Ollama ping, Google check) does not run
    wake_checks()
    route_checks(c)
    google_checks()
    model_checks(c)
    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:400]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} wake checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
