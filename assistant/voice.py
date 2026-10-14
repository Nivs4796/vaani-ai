"""Voice on this Mac: speech-to-text with faster-whisper, text-to-speech with macOS `say`.

Audio never leaves the laptop and is never written to disk: the browser posts the
recording, transcribe() decodes it from memory (PyAV reads webm/opus, ogg, mp4 and
wav from bytes) and the model runs on the CPU. The model loads once, in a daemon
thread started by start_warmup(), from the local Hugging Face cache only
(local_files_only=True), so the running server makes no network call; fetch it once
with `python -m assistant.voice fetch`.

spoken_text(result) builds what Vaani says aloud, in code (voice rule 3): no markdown,
URLs, ids or email addresses, at most two sentences of the reply, one sentence for a
card, and never "sent", "done" or "created" without a receipt Google verified.

Env, read at call time: VAANI_VOICE (default 1; 0 = off), VAANI_STT_MODEL (small.en),
VAANI_STT_COMPUTE (int8), VAANI_STT_THREADS (8), VAANI_TTS_VOICE (Samantha),
VAANI_TTS_RATE (185 words/min).

CLI: python -m assistant.voice status|fetch|say <text>
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("assistant.voice")

MAX_TTS_CHARS = 600
SPOKEN_MAX_CHARS = 220
# Effects voices in `say -v ?` (bells, organ, robots): not a voice for an assistant.
NOVELTY_VOICES = frozenset({"Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Good News", "Jester",
                            "Organ", "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox"})

_lock = threading.Lock()  # guards _state and the model handle
_model_lock = threading.Lock()  # one transcription at a time on the shared model
_model = None
_state: dict = {"stt": "idle", "model": None, "error": None, "load_ms": None}
_voices_cache: list[dict] | None = None


# --------------------------------------------------------------------------- config

def enabled() -> bool:
    return os.environ.get("VAANI_VOICE", "1").strip().lower() not in ("0", "false", "no", "off")


def stt_model_name() -> str:
    return os.environ.get("VAANI_STT_MODEL", "").strip() or "small.en"


def stt_compute() -> str:
    return os.environ.get("VAANI_STT_COMPUTE", "").strip() or "int8"


def stt_threads() -> int:
    """CPU threads for the model; 8 cut a 2 s clip from about 830 ms to 660 ms on an M5."""
    try:
        return max(1, int(os.environ.get("VAANI_STT_THREADS", "").strip()))
    except ValueError:
        return min(8, os.cpu_count() or 4)


def tts_voice() -> str:
    return os.environ.get("VAANI_TTS_VOICE", "").strip() or "Samantha"


def tts_rate() -> int:
    try:
        return max(90, min(360, int(os.environ.get("VAANI_TTS_RATE", "185").strip())))
    except ValueError:
        return 185


# --------------------------------------------------------------------------- speech-to-text

def status() -> dict:
    """{"stt": "off"|"loading"|"ready"|"error", "model", "error", "load_ms"}."""
    if not enabled():
        return {"stt": "off", "model": stt_model_name(), "error": None, "load_ms": None}
    with _lock:
        out = dict(_state)
    if out["stt"] == "idle":  # nobody started the warm-up yet; the first request starts it
        out["stt"] = "loading"
    out["model"] = out["model"] or stt_model_name()
    return out


def _load() -> None:
    name, compute = stt_model_name(), stt_compute()
    t0 = time.perf_counter()
    try:
        try:
            import numpy as np
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(f"faster-whisper is not installed ({e}); run: pip install -r requirements.txt") from e
        try:
            model = WhisperModel(name, device="cpu", compute_type=compute, cpu_threads=stt_threads(),
                                 local_files_only=True)
        except Exception as e:
            if re.search(r"NotFound|Offline|cache|local", f"{type(e).__name__} {e}", re.I):
                raise RuntimeError(f"speech model {name!r} is not in the local Hugging Face cache "
                                   f"({type(e).__name__}); download it once with: python -m assistant.voice fetch") from e
            raise RuntimeError(f"could not load speech model {name!r}: {type(e).__name__}: {e}") from e
        # Transcription is lazy: list() makes the warm-up really run the encoder and decoder once.
        segments, _info = model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1,
                                           condition_on_previous_text=False)
        list(segments)
    except Exception as e:
        msg = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
        log.error("speech-to-text did not load: %s", msg)
        with _lock:
            _state.update({"stt": "error", "model": name, "error": msg, "load_ms": None})
        return
    global _model
    ms = int((time.perf_counter() - t0) * 1000)
    with _lock:
        _model = model
        _state.update({"stt": "ready", "model": name, "error": None, "load_ms": ms})
    log.info("speech-to-text ready: %s (%s, cpu) in %d ms", name, compute, ms)


def start_warmup() -> None:
    """Load the speech-to-text model in a daemon thread. A no-op when voice is off, or while
    it is loading or ready; after an error it tries again."""
    if not enabled():
        return
    with _lock:
        if _state["stt"] in ("loading", "ready"):
            return
        _state.update({"stt": "loading", "model": stt_model_name(), "error": None, "load_ms": None})
    threading.Thread(target=_load, name="vaani-stt-warmup", daemon=True).start()


def ready() -> bool:
    return enabled() and status()["stt"] == "ready"


def transcribe(data: bytes) -> dict:
    """Audio bytes (webm/opus, ogg, mp4 or wav) -> {"text", "ms", "model", "empty"}.
    Decoded from memory; nothing is written to disk. Raises RuntimeError if the model is not ready."""
    with _lock:
        model, name = _model, _state["model"] or stt_model_name()
    if model is None:
        raise RuntimeError("speech-to-text is not ready")
    t0 = time.perf_counter()
    with _model_lock:
        segments, _info = model.transcribe(
            io.BytesIO(data), language="en", beam_size=1, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments if s.text and s.text.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return {"text": text, "ms": int((time.perf_counter() - t0) * 1000), "model": name, "empty": not text}


# --------------------------------------------------------------------------- text-to-speech

_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<lang>[a-z]{2,3}_[A-Za-z0-9]{2,4})\s+#")


def parse_voices(listing: str) -> list[dict]:
    """`say -v '?'` output -> [{"name", "lang"}]: English voices plus hi_IN, effects voices
    left out, each name once."""
    out, seen = [], set()
    for line in (listing or "").splitlines():
        m = _VOICE_LINE.match(line.strip())
        if not m:
            continue
        name, lang = m.group("name").strip(), m.group("lang")
        if not (lang.startswith("en_") or lang == "hi_IN") or name in NOVELTY_VOICES or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "lang": lang})
    return out


def voices() -> list[dict]:
    """Installed `say` voices (English and hi_IN), read once per process."""
    global _voices_cache
    if _voices_cache is not None:
        return list(_voices_cache)
    say = shutil.which("say")
    if not say:
        return []
    try:
        proc = subprocess.run([say, "-v", "?"], capture_output=True, text=True, timeout=10)
        found = parse_voices(proc.stdout)
    except (OSError, subprocess.SubprocessError) as e:
        log.error("could not list say voices: %s", e)
        return []
    _voices_cache = found
    return list(found)


def resolve_voice(name: str | None) -> str | None:
    """An installed voice for name (exact, or "Samantha" for "Samantha (English (US))"),
    else the same for VAANI_TTS_VOICE, else None (the system voice)."""
    names = [v["name"] for v in voices()]
    for want in (name, tts_voice()):
        w = (want or "").strip().lower()
        if not w:
            continue
        for n in names:
            if n.lower() == w:
                return n
        for n in names:
            if n.lower().split(" (", 1)[0] == w:
                return n
    return None


def synth(text: str, voice: str | None = None) -> bytes:
    """Text -> WAV bytes (PCM16 mono 22050 Hz) from macOS `say`. Only installed voices are
    used; the text is capped at 600 characters. Raises RuntimeError on failure."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_TTS_CHARS]
    if not text:
        raise ValueError("nothing to say")
    say = shutil.which("say")
    if not say:
        raise RuntimeError("macOS `say` is not available on this machine")
    cmd = [say]
    picked = resolve_voice(voice)
    if picked:
        cmd += ["-v", picked]
    with tempfile.TemporaryDirectory(prefix="vaani-tts-") as tmp:
        out = Path(tmp) / "speech.wav"
        cmd += ["-r", str(tts_rate()), "-o", str(out), "--file-format=WAVE", "--data-format=LEI16@22050", "--", text]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("say took longer than 30 s") from e
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"say failed ({proc.returncode}): {(proc.stderr or '').strip()[:200]}")
        data = out.read_bytes()
        out.unlink(missing_ok=True)
    return data


# --------------------------------------------------------------------------- what Vaani says aloud

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()\s]|\([^()]*\))*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMAIL = re.compile(r"<?\b[\w.+%-]+@[\w-]+(?:\.[\w-]+)+\b>?")
_HEXID = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b", re.I)
_MIXEDID = re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{12,}\b")
_DRAFTID = re.compile(r"\b(?:draft(?:_id)?[\s:#=]*)?d(?=[0-9a-f]{0,3}\d)[0-9a-f]{4}\b", re.I)
_LIST = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+")
_CLAIM = re.compile(r"\b(?:sent|done|created)\b", re.I)
_CLICK = re.compile(r"\bclick(?:s|ing)?\b|\bon the card\b", re.I)
_DRAFTED = re.compile(r"\bdrafted\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def speakable(text: str) -> str:
    """Markdown, links, URLs, email addresses and ids out; list items folded into one line."""
    text = _MD_LINK.sub(lambda m: m.group(1), str(text or ""))
    text = _URL.sub("", text)
    text = _EMAIL.sub("", text)
    text = _DRAFTID.sub("", text)
    text = _HEXID.sub("", text)
    text = _MIXEDID.sub("", text)
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)
    text = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)_(?!\w)", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = _EMOJI.sub("", text)
    parts: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s+|>\s*)", "", raw)
        is_item = bool(_LIST.match(line))
        line = _LIST.sub("", line).strip(" \t*")
        if not re.search(r"\w", line):  # blank lines, "---" rules
            continue
        if is_item and parts:
            prev = parts[-1].rstrip()
            sep = " " if prev.endswith(":") else ", "
            parts[-1] = prev.rstrip(".;,") + sep + line.rstrip(".;,")
        else:
            parts.append(line)
    sentences = [p if p[-1] in ".!?:" else p + "." for p in (x.strip() for x in parts) if p]
    out = " ".join(s[:-1] + "." if s.endswith(":") else s for s in sentences)
    out = out.replace("(s)", "s")
    out = re.sub(r"\s*[•·|]\s*", ", ", out)
    out = re.sub(r"\(\s*\)|\[\s*\]", "", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;:])(?=[.!?])", "", out)
    out = re.sub(r"\.{2,}", ".", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,;")


def receipt_ok(rc) -> bool:
    """Same tone rule as the page's receipt card: verified by Google and not a failure."""
    if not isinstance(rc, dict):
        return False
    if re.search(r"ERROR|FAIL|NOT", str(rc.get("status") or ""), re.I):
        return False
    v = rc.get("verified")
    if isinstance(v, bool) or v is None:
        return v is True
    if isinstance(v, (int, float)):
        count = rc.get("count")
        return v >= count if count else v > 0
    return False


def _cap(sentences: list[str], limit: int = SPOKEN_MAX_CHARS) -> str:
    out = " ".join(sentences[:2])
    if len(out) > limit and len(sentences) > 1:
        out = sentences[0]
    if len(out) > limit:
        cut = out[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
        out = cut + "."
    return out


# Card titles (manage.py, agent.py) as a phrase after "I've drafted".
_CARD_PHRASES = [(re.compile(p, re.I), r) for p, r in (
    (r"^email draft$", "an email"), (r"^(reply|forward|follow-up) draft$", r"a \1"),
    (r"^new event:\s*(.+)$", r"a new event, \1"), (r"^change event$", "a change to the event"),
    (r"^delete event$", "removing the event"), (r"^answer invite:\s*(.+)$", r"your answer to the invite, \1"),
    (r"^(.+?):\s*(\d+ emails?)$", r"a change to \1 \2"),
)]


def _card_sentence(card: dict) -> str:
    title = speakable(str(card.get("title") or "")).rstrip(".")
    title = re.sub(r"^draft:\s*", "", title, flags=re.I).strip() or "it"
    title = title[0].lower() + title[1:]
    for pattern, repl in _CARD_PHRASES:
        title, n = pattern.subn(repl, title)
        if n:
            break
    label = speakable(str(card.get("confirm_label") or "Confirm")).rstrip(".") or "Confirm"
    return f"I've drafted {title}. Tap {label} when you're ready."


def spoken_text(result: dict | None) -> str:
    """What Vaani says for a turn result: at most two sentences of the reply (about 220
    characters), plus one sentence for a waiting card ("I've drafted a reply. Tap Send when
    you're ready."), which replaces the reply's own "click Send on the card". Built in code,
    never by the model."""
    if not isinstance(result, dict):
        return ""
    claims_ok = receipt_ok(result.get("receipt"))
    body = speakable(result.get("reply") or "")
    sentences = [s.strip() for s in _SENTENCE.split(body) if s.strip() and re.search(r"\w", s)]
    if not claims_ok:  # no verified receipt: never let a sentence claim it was sent, done or created
        sentences = [s for s in sentences if not _CLAIM.search(s)]
    draft = result.get("pending_draft")
    card = draft.get("card") if isinstance(draft, dict) else None
    extra = ""
    if isinstance(card, dict) and card.get("title"):
        # The card sentence says how to go ahead; the reply's own "click Send on the card" goes.
        sentences = [s for s in sentences if not _CLICK.search(s)]
        extra = _card_sentence(card)
        if any(_DRAFTED.search(s) for s in sentences[:2]):  # "I've drafted a reply to Ravi." already said it
            extra = extra.split(". ", 1)[-1]
        if not claims_ok and _CLAIM.search(extra):
            extra = ""
    out = _cap(sentences) if sentences else ""
    return f"{out} {extra}".strip()


# --------------------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "fetch":  # the one network call: download the model into the Hugging Face cache
        from faster_whisper import WhisperModel

        WhisperModel(stt_model_name(), device="cpu", compute_type=stt_compute())
        print(f"{stt_model_name()} is in the local cache; set HF_HUB_OFFLINE=1 to keep it offline")
        return 0
    if cmd == "say":
        sys.stdout.buffer.write(synth(" ".join(argv[1:]) or "Hello from Vaani."))
        return 0
    start_warmup()
    while status()["stt"] == "loading":
        time.sleep(0.2)
    print(status())
    print(f"tts: say, voice {resolve_voice(None)}, {len(voices())} voices")
    return 0 if status()["stt"] in ("ready", "off") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
