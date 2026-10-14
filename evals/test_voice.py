"""Offline checks for voice: spoken_text() (voice rule 3), the `say` voice list and WAV
output, the /voice/* routes (single mode, TestClient), voice rule 2 (decide() keeps an
asked-for card waiting even at Auto) and, when the model is in the local cache, a real
transcription of the synthesized fixtures in static/test/.

No network: the model loads with local_files_only=True and HF_HUB_OFFLINE=1.

Run: .venv/bin/python -m evals.test_voice   (exit 1 on any failure)
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

# Set before any assistant import (python-dotenv would fill unset ones from .env).
os.environ.update({"VAANI_AUTH": "single", "VAANI_VOICE": "1", "VAANI_STT_MODEL": "small.en", "VAANI_STT_COMPUTE": "int8",
                   "VAANI_TTS_VOICE": "Samantha", "VAANI_TTS_RATE": "185", "HF_HUB_OFFLINE": "1"})

from fastapi.testclient import TestClient  # noqa: E402

from assistant import agent, autonomy, voice, web  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "static" / "test"
Q1, Q2 = "What's on my calendar today?", "Who is waiting on a reply from me?"

CHECKS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


def words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


def overlap(heard: str, want: str) -> float:
    w, h = words(want), set(words(heard))
    return sum(1 for x in w if x in h) / max(1, len(w))


def card(title: str, label: str = "Confirm") -> dict:
    return {"draft_id": "d1a2b", "kind": "email_send", "card": {"title": title, "confirm_label": label, "lines": []}}


def spoken_checks() -> None:
    st = voice.spoken_text
    s = st({"reply": "**Ravi Mehta** wants the _quote_ by Friday.\n- Item one\n- Item two"})
    check("markdown stripped", "*" not in s and "_" not in s and "Ravi Mehta wants the quote" in s and "-" not in s, s)
    s = voice.speakable("## Today \U0001F4C5\n---\n> Standup · 10:00 ✅\n`code` here.")
    check("headings, rules, quotes, emoji and middots", s == "Today. Standup, 10:00. code here.", s)
    s = st({"reply": "Here is your day:\n1. Standup at 10\n2. Lunch with Paula Sanders at 1"})
    check("list folded into one sentence", s == "Here is your day: Standup at 10, Lunch with Paula Sanders at 1.", s)
    s = st({"reply": "See [the invite](https://calendar.example.org/event?eid=abc123) for details."})
    check("[label](url) becomes label", s == "See the invite for details.", s)
    s = st({"reply": "Open https://mail.example.org/mail/u/0/#inbox/18c2f3a4b5d6e7f8 to read it."})
    check("bare URL dropped", "http" not in s and "example.org" not in s and s.startswith("Open"), s)
    s = st({"reply": "Sam Lee <sam.lee@example.org> asked about lunch, cc paula@example.org."})
    check("email addresses dropped", "@" not in s and "example.org" not in s and s.startswith("Sam Lee asked about lunch"), s)
    s = st({"reply": "Message 18c2f3a4b5d6e7f8 from Ravi Mehta is about the quote (draft d3fa2), event 7k2jh4g5f6d8s9a0q1."})
    check("ids dropped", not re.search(r"18c2f3|d3fa2|7k2jh4", s) and "Ravi Mehta" in s, s)
    s = st({"reply": "First sentence. Second sentence! Third sentence? Fourth."})
    check("at most two sentences", s == "First sentence. Second sentence!", s)
    long = "This is a very long sentence about the quarterly design package " * 6
    s = st({"reply": long.strip() + ". Short one."})
    check("about 220 characters at most", 0 < len(s) <= 221 and s.endswith("."), f"{len(s)}: {s}")
    s = st({"reply": "Ravi Mehta wants to meet on Friday.", "pending_draft": card("Draft: Lunch with Ravi Mehta", "Confirm")})
    check("card sentence added", s == "Ravi Mehta wants to meet on Friday. I've drafted lunch with Ravi Mehta. Tap Confirm when you're ready.", s)
    s = st({"reply": "", "pending_draft": card("Reply draft", "Send")})
    check("card sentence names the kind of draft", s == "I've drafted a reply. Tap Send when you're ready.", s)
    s = st({"reply": "Here it is. Click Send on the card when you are happy with it.", "pending_draft": card("New event: Review with Sam Lee")})
    check("the reply's click instruction gives way to the card sentence",
          s == "Here it is. I've drafted a new event, Review with Sam Lee. Tap Confirm when you're ready.", s)
    s = st({"reply": "I've drafted a reply to Ravi Mehta.", "pending_draft": card("Reply draft", "Send")})
    check("no second 'I've drafted'", s == "I've drafted a reply to Ravi Mehta. Tap Send when you're ready.", s)
    s = st({"reply": "Archive these?", "pending_draft": card("Archive: 3 email(s)", "Archive 3")})
    check("(s) read as a plural", s == "Archive these? I've drafted a change to archive 3 emails. Tap Archive 3 when you're ready.", s)
    s = st({"reply": "", "pending_draft": card("Answer invite: accepted", "Accepted")})
    check("invite card phrase", s == "I've drafted your answer to the invite, accepted. Tap Accepted when you're ready.", s)
    claim = "I've sent the email to Sam Lee. The meeting is created and it's done. Anything else?"
    for name, rc in (("no receipt", None), ("unverified receipt", {"status": "SENT", "verified": False}),
                     ("failed receipt", {"status": "ERROR", "verified": True}),
                     ("partly verified", {"status": "OK", "verified": 1, "count": 2})):
        s = st({"reply": claim, "receipt": rc})
        check(f"never sent/done/created with {name}", not re.search(r"\b(sent|done|created)\b", s, re.I) and s == "Anything else?", s)
    s = st({"reply": claim, "receipt": {"status": "SENT", "verified": True, "report": ["Sent to Sam Lee"]}})
    check("sent allowed with a verified receipt", "sent" in s and "Sam Lee" in s and "@" not in s, s)
    s = st({"reply": "Nothing has been sent yet: it waits for your click.", "pending_draft": card("Email draft", "Send")})
    check("'nothing has been sent' is not spoken either", "sent" not in s and s == "I've drafted an email. Tap Send when you're ready.", s)
    check("empty results speak nothing", st(None) == "" and st({}) == "" and st({"reply": ""}) == "" and st({"reply": "**  **"}) == "")


def voices_checks() -> None:
    listing = "\n".join([
        "Albert              en_US    # Hello! My name is Albert.",
        "Alice               it_IT    # Ciao! Mi chiamo Alice.",
        "Bubbles             en_US    # Hello! My name is Bubbles.",
        "Lekha               hi_IN    # नमस्ते, मेरा नाम लेखा है।",
        "Rishi (English (India)) en_IN    # Hello! My name is Rishi.",
        "Samantha (English (US)) en_US    # Hello! My name is Samantha.",
        "Samantha (English (US)) en_US    # Hello! My name is Samantha.",
        "Soumya              kn_IN    # Hello! My name is Soumya.",
        "garbage line",
    ])
    got = voice.parse_voices(listing)
    want = [{"name": "Albert", "lang": "en_US"}, {"name": "Lekha", "lang": "hi_IN"},
            {"name": "Rishi (English (India))", "lang": "en_IN"}, {"name": "Samantha (English (US))", "lang": "en_US"}]
    check("say -v ? parsed (English + hi_IN, once each, no effects voices)", got == want, got)
    live = voice.voices()
    check("installed voices listed", live and all(v["lang"].startswith("en_") or v["lang"] == "hi_IN" for v in live), live[:5])
    check("default voice resolves (Samantha -> its full name)", (voice.resolve_voice(None) or "").startswith("Samantha"), voice.resolve_voice(None))
    check("unknown voice falls back to the default", voice.resolve_voice("No Such Voice") == voice.resolve_voice(None))
    t0 = time.perf_counter()
    wav = voice.synth("Hello from Vaani. " + "x" * 2000)
    ms = int((time.perf_counter() - t0) * 1000)
    check("synth returns RIFF/WAVE PCM16 mono 22050 Hz", wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
          and int.from_bytes(wav[22:24], "little") == 1 and int.from_bytes(wav[24:28], "little") == 22050
          and int.from_bytes(wav[34:36], "little") == 16, wav[:40])
    check("synth with an unknown voice still speaks", voice.synth("Hi.", "No Such Voice")[:4] == b"RIFF")
    print(f"synth: {len(wav)} bytes in {ms} ms")


def route_checks(c: TestClient) -> None:
    os.environ["VAANI_VOICE"] = "0"
    r = c.post("/voice/stt", content=b"x" * 10, headers={"Content-Type": "audio/webm"})
    check("stt 503 when voice is off", r.status_code == 503 and r.json() == {"error": "voice is off"}, r.text)
    check("status says off", c.get("/voice/status").json().get("stt") == "off")
    check("tts 503 when voice is off", c.post("/voice/tts", json={"text": "hi"}).status_code == 503)
    os.environ["VAANI_VOICE"] = "1"
    r = c.post("/voice/stt", content=b"x" * (2 * 1024 * 1024 + 1), headers={"Content-Type": "audio/webm"})
    check("stt 413 over 2 MB", r.status_code == 413, r.status_code)
    r = c.post("/voice/stt", content=b"hello", headers={"Content-Type": "text/plain"})
    check("stt 415 for text/plain", r.status_code == 415, r.status_code)
    r = c.post("/voice/stt", json={"audio": "x"})
    check("stt 415 for JSON", r.status_code == 415, r.status_code)
    real_status, real_start = voice.status, voice.start_warmup
    started = []
    voice.status = lambda: {"stt": "loading", "model": "small.en", "error": None, "load_ms": None}
    voice.start_warmup = lambda: started.append(1)
    try:
        r = c.post("/voice/stt", content=b"RIFF", headers={"Content-Type": "audio/wav"})
        check("stt 503 warming up", r.status_code == 503 and r.json() == {"error": "warming up"}, r.text)
        check("a request while idle starts the warm-up", started == [1], started)
        st = c.get("/voice/status").json()
        check("status route shape", st.get("stt") == "loading" and st.get("tts") == "say" and st.get("voice", "").startswith("Samantha")
              and isinstance(st.get("voices"), list) and st["voices"] and {"name", "lang"} <= set(st["voices"][0]), st)
    finally:
        voice.status, voice.start_warmup = real_status, real_start
    r = c.post("/voice/tts", json={"text": "Hello **Sam Lee**, see https://example.org/x"})
    spoken = unquote(r.headers.get("x-vaani-spoken", ""))
    check("tts returns audio/wav", r.status_code == 200 and r.headers.get("content-type", "").startswith("audio/wav")
          and r.content[:4] == b"RIFF", (r.status_code, r.headers.get("content-type")))
    check("tts X-Vaani-Spoken is the cleaned text", spoken == "Hello Sam Lee, see.", spoken)
    result = {"reply": "I've sent it to sam.lee@example.org. Click Send on the card.", "pending_draft": card("Reply draft", "Send"),
              "receipt": None}
    r = c.post("/voice/tts", json={"result": result, "voice": "Tara"})
    spoken = unquote(r.headers.get("x-vaani-spoken", ""))
    check("tts with a result speaks spoken_text()", r.status_code == 200 and spoken == "I've drafted a reply. Tap Send when you're ready.", spoken)
    check("tts with nothing to say is 204", c.post("/voice/tts", json={"text": "  "}).status_code == 204
          and c.post("/voice/tts", json={"result": {"reply": "**"}}).status_code == 204)


def autonomy_checks() -> None:
    """Voice rule 2: a spoken (or typed) request makes a card with origin "asked", and
    decide() keeps it waiting for a click even with the workflow at Auto and sending unlocked."""
    saved = {k: os.environ.get(k) for k in ("VAANI_AUTONOMY", "VAANI_AUTO_ALLOWED")}
    os.environ.update({"VAANI_AUTONOMY": "1", "VAANI_AUTO_ALLOWED": "replies,follow_ups,scheduling,triage"})
    try:
        prefs = {"autonomy": {wf: {"level": "auto", "send_unlocked": True} for wf in autonomy.WORKFLOWS}}
        cards = {
            "reply": {"kind": "email_send", "to": ["sam.lee@example.org"], "subject": "Lunch", "body": "Friday works."},
            "event": {"kind": "event_create", "summary": "Review with Ravi Mehta", "start": "2026-10-15T10:00", "end": "2026-10-15T10:30"},
            "archive": {"kind": "email_modify", "action": "archive", "ids": ["m1"]},
        }
        for name, d in cards.items():
            asked = dict(d, origin="asked")
            check(f"voice never runs Auto: asked {name} card waits", autonomy.decide(asked, prefs) == "wait", autonomy.decide(asked, prefs))
            untagged = dict(d)  # no origin at all counts as asked
            check(f"untagged {name} card waits", autonomy.decide(untagged, prefs) == "wait")
            control = dict(d, origin="brief")
            check(f"control: the same {name} card from the brief is auto", autonomy.decide(control, prefs) == "auto", autonomy.decide(control, prefs))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("voice rule 1: a bare spoken 'send it' is a confirmation the guard intercepts",
          agent.is_confirmation("send it") and agent.is_confirmation("Yes, send it.") and not agent.is_confirmation("yes, but make it 4pm"))


def model_checks(c: TestClient) -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError as e:
        print(f"SKIPPED  real transcription: faster-whisper is not installed ({e})")
        return
    voice.start_warmup()
    t0 = time.perf_counter()
    while voice.status()["stt"] == "loading" and time.perf_counter() - t0 < 180:
        time.sleep(0.1)
    st = voice.status()
    if st["stt"] == "error" and "not in the local Hugging Face cache" in (st.get("error") or ""):
        print(f"SKIPPED  real transcription: {st['error']}")
        return
    check("model loads from the local cache", st["stt"] == "ready", st)
    if st["stt"] != "ready":
        return
    for fname, want in (("voice-q1.wav", Q1), ("voice-q2.wav", Q2), ("voice-q1.webm", Q1)):
        data = (FIXTURES / fname).read_bytes()
        res = voice.transcribe(data)
        check(f"transcribe {fname} (>= 80% of the words)", overlap(res["text"], want) >= 0.8 and not res["empty"], res)
        print(f"stt {fname}: {res['ms']} ms -> {res['text']!r}")
    ctype = {"wav": "audio/wav", "webm": "audio/webm;codecs=opus"}
    for fname, want in (("voice-q1.webm", Q1), ("voice-q2.wav", Q2)):
        r = c.post("/voice/stt", content=(FIXTURES / fname).read_bytes(), headers={"Content-Type": ctype[fname.rsplit(".", 1)[1]]})
        body = r.json() if r.status_code == 200 else {}
        check(f"POST /voice/stt {fname}", r.status_code == 200 and overlap(body.get("text", ""), want) >= 0.8
              and set(body) == {"text", "ms", "model", "empty"}, (r.status_code, r.text[:200]))
    r = c.post("/voice/stt", content=b"this is not audio at all", headers={"Content-Type": "audio/webm"})
    check("undecodable audio is a 415, not a 500", r.status_code == 415, (r.status_code, r.text[:200]))
    r = c.post("/voice/stt", content=b"", headers={"Content-Type": "audio/webm"})
    check("an empty recording is empty text", r.status_code == 200 and r.json().get("empty") is True, r.text[:200])


def main() -> int:
    c = TestClient(web.app)  # no `with`: the lifespan (Ollama ping, Google check) does not run
    spoken_checks()
    voices_checks()
    route_checks(c)
    autonomy_checks()
    model_checks(c)
    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:400]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} voice checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
