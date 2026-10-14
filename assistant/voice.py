"""Voice on this Mac: speech-to-text with faster-whisper, text-to-speech with macOS `say`.

Audio never leaves the laptop and is never written to disk: the browser posts the
recording, transcribe() decodes it from memory (PyAV reads webm/opus, ogg, mp4 and
wav from bytes) and the model runs on the CPU. The model loads once, in a daemon
thread started by start_warmup(), from the local Hugging Face cache only
(local_files_only=True), so the running server makes no network call; fetch it once
with `python -m assistant.voice fetch`.

transcribe(data, purpose) also says how sure the model was (confidence 0..1, see
confidence_of) and how long the clip was; purpose "wake" is the fast setting for the short
clips the page sends while it waits for "Hey Vaani", and wake_match() finds the name in the
first words. The spoken cues ("On it.", "Checking your calendar.") are rendered once with
`say` at warm-up and served from memory (cue, cue_for_tool).

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
import math
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
    """Load the speech-to-text model in a daemon thread, and render the spoken cues in another.
    A no-op when voice is off, or while it is loading or ready; after an error it tries again.
    Never blocks: a cue that fails to render is just missing."""
    if not enabled():
        return
    _start_cues()
    with _lock:
        if _state["stt"] in ("loading", "ready"):
            return
        _state.update({"stt": "loading", "model": stt_model_name(), "error": None, "load_ms": None})
    threading.Thread(target=_load, name="vaani-stt-warmup", daemon=True).start()


def ready() -> bool:
    return enabled() and status()["stt"] == "ready"


# Context for the decoder. A neutral "Hey Vaani." (no approval word in it, so it never
# nudges the model towards "confirm" or "send it") lifted the confidence of one-word
# commands from about 0.45 to 0.75 on the `say` fixtures and spells the name the same way
# every time. The wake prompt adds the most common thing said after the name.
COMMAND_PROMPT = "Hey Vaani."
WAKE_PROMPT = "Hey Vaani, brief me."
WAKE_MAX_TOKENS = 24  # a wake segment is at most about 3 s: "Hey Vaani" and a short command
NO_TIMESTAMPS_UNDER_S = 28.0  # one 30 s window: timestamp tokens only cost time (and confidence)


def confidence_of(segments) -> float:
    """How sure the model is of what it wrote, 0..1. Formula: the mean over segments,
    weighted by each segment's token count, of exp(avg_logprob) * (1 - no_speech_prob).
    exp(avg_logprob) is the geometric-mean probability of the segment's tokens; the second
    factor discounts segments the model thinks are not speech at all. No text -> 0."""
    total = weight = 0.0
    for s in segments:
        if not (s.text and s.text.strip()):
            continue
        n = max(1, len(getattr(s, "tokens", None) or ()))
        p = math.exp(min(0.0, float(s.avg_logprob))) * (1.0 - min(1.0, max(0.0, float(s.no_speech_prob))))
        total += p * n
        weight += n
    return round(total / weight, 3) if weight else 0.0


def transcribe(data: bytes, purpose: str = "command") -> dict:
    """Audio bytes (webm/opus, ogg, mp4 or wav) -> {"text", "ms", "model", "empty", "confidence",
    "duration_ms"}. purpose "wake" is tuned for a short standby clip: wake prompt, at most
    WAKE_MAX_TOKENS, no fallback temperatures. Decoded from memory; nothing is written to disk.
    Raises RuntimeError if the model is not ready."""
    with _lock:
        model, name = _model, _state["model"] or stt_model_name()
    if model is None:
        raise RuntimeError("speech-to-text is not ready")
    from faster_whisper.audio import decode_audio

    t0 = time.perf_counter()
    audio = decode_audio(io.BytesIO(data), sampling_rate=16000)
    duration = len(audio) / 16000
    opts = {"language": "en", "beam_size": 1, "condition_on_previous_text": False, "vad_filter": True,
            "without_timestamps": duration <= NO_TIMESTAMPS_UNDER_S}
    if purpose == "wake":
        opts.update(initial_prompt=WAKE_PROMPT, max_new_tokens=WAKE_MAX_TOKENS, temperature=0.0,
                    vad_parameters={"min_silence_duration_ms": 300})
    else:
        opts.update(initial_prompt=COMMAND_PROMPT, vad_parameters={"min_silence_duration_ms": 500})
    with _model_lock:
        segments, _info = model.transcribe(audio, **opts)
        segments = [s for s in segments if s.text and s.text.strip()]
    text = re.sub(r"\s+", " ", " ".join(s.text.strip() for s in segments)).strip()
    return {"text": text, "ms": int((time.perf_counter() - t0) * 1000), "model": name, "empty": not text,
            "confidence": confidence_of(segments) if text else 0.0, "duration_ms": int(round(duration * 1000))}


# --------------------------------------------------------------------------- wake word

_WORD = re.compile(r"[A-Za-z0-9']+")
_GREETINGS = frozenset({"hey", "hi", "hay", "hae", "hei", "heyy", "hej", "ey", "eh", "ay", "a", "hello", "hallo"})
_FILLERS = frozenset({"um", "uh", "umm", "erm", "oh", "okay", "ok"})
_MERGED_PREFIXES = ("hey", "hay", "hae", "hei", "hi", "ha", "a")  # "Haevani", "Heyvaani", "Avani"
_BARE_NAMES = frozenset({"vaani", "vani", "vanni"})  # the name alone, spelled as Whisper writes it
_REST_TRIM = " \t\r\n,.;:!?-—–…\"'“”‘’()"


def _name_key(word: str) -> str:
    """A rough sound key for the name: "Vaani", "Vani", "Varni", "Bani", "Vaney", "Vahni"
    and "Vanny" all become "vani"; "funny" (funi), "bunny" (vuni), "Benny" (veni),
    "honey" (oni) and "many" (mani) do not."""
    w = word.lower().replace("'", "")
    w = w.replace("ph", "f").replace("w", "v").replace("b", "v").replace("h", "")
    w = re.sub(r"(.)\1+", r"\1", w)  # vaani -> vani, vanni -> vani
    w = re.sub(r"ar(?=n)", "a", w)  # varni -> vani, barney -> vaney
    return re.sub(r"(?:ey|ee|ie|y|e)$", "i", w)  # vaney -> vani, vany -> vani


def _is_name(word: str) -> bool:
    return _name_key(word) == "vani"


def wake_match(text: str) -> tuple[bool, str]:
    """(matched, rest): does the text start with "Hey Vaani" as Whisper writes it ("Hey
    Vaani", "Hey Vani", "Hey Varni", "Hey Bani", "Hi Vaani", "A Vaani", "Haevani", "Hey,
    Vaani")? Looks at the first one to three words only, after one filler ("um", "oh",
    "okay"). rest = the words after the name, punctuation trimmed ("" for a bare wake).
    Ordinary speech ("Have any of you", "Hey you", "Hey funny", "Hello there") is rejected."""
    src = str(text or "")
    words = list(_WORD.finditer(src))
    low = [m.group(0).lower() for m in words]
    i = 1 if low and low[0] in _FILLERS else 0
    end = None  # index of the last word that belongs to the wake phrase
    if i < len(low):
        w = low[i]
        if w in _GREETINGS and i + 1 < len(low):
            if _is_name(low[i + 1]):
                end = i + 1
            elif i + 2 < len(low) and len(low[i + 1]) <= 3 and _is_name(low[i + 1] + low[i + 2]):
                end = i + 2  # "Hey Va ni"
        if end is None and w in _BARE_NAMES:
            end = i
        if end is None:
            for p in _MERGED_PREFIXES:
                if w.startswith(p) and len(w) > len(p) + 2 and _is_name(w[len(p):]):
                    end = i
                    break
    if end is None:
        return False, ""
    rest = src[words[end].end():].strip(_REST_TRIM)
    return True, re.sub(r"\s+", " ", rest)


# --------------------------------------------------------------------------- spoken cues

# Short clips the page plays while Vaani works, rendered once with synth() at warm-up.
CUES: dict[str, str] = {
    "on_it": "On it.",
    "one_moment": "One moment.",
    "still_working": "Still working on it.",
    "checking_calendar": "Checking your calendar.",
    "reading_inbox": "Reading your inbox.",
    "opening_email": "Opening the email.",
    "writing_draft": "Writing the draft.",
    "looking_free_time": "Looking for free time.",
    "saving": "Saving to Google.",
    "stopped": "Stopped.",
    "cancelled": "Cancelled. Nothing was changed.",
    "didnt_catch": "Sorry, I didn't catch that.",
}
# steps.py tool -> cue; any other tool -> still_working.
TOOL_CUES: dict[str, str] = {
    "list_calendar_events": "checking_calendar",
    "list_recent_emails": "reading_inbox",
    "read_email": "opening_email",
    "draft_email": "writing_draft",
    "draft_nudge": "writing_draft",
    "find_free_time": "looking_free_time",
    "commit_pending": "saving",
    "create_calendar_event": "saving",
}
_cues: dict[str, bytes] = {}
_cue_state = {"started": False}


def cue(name: str) -> bytes | None:
    """The rendered WAV for a cue name, or None (unknown, not rendered yet, or `say` failed)."""
    with _lock:
        return _cues.get(str(name))


def cue_names() -> list[str]:
    """Cue names rendered so far, in CUES order."""
    with _lock:
        return [n for n in CUES if n in _cues]


def cue_for_tool(tool: str | None) -> str | None:
    """The progress cue for a running tool: None without a tool name, still_working for one not mapped."""
    if not tool:
        return None
    return TOOL_CUES.get(str(tool), "still_working")


def _render_cues() -> None:
    for name, text in CUES.items():
        if cue(name) is not None:
            continue
        try:
            wav = synth(text)
        except Exception as e:  # that cue stays missing; GET /voice/cue/<name> answers 503
            log.error("cue %s did not render: %s: %s", name, type(e).__name__, e)
            continue
        with _lock:
            _cues[name] = wav
    with _lock:
        _cue_state["started"] = False  # a later start_warmup() retries any that failed
    log.info("voice cues ready: %d of %d", len(cue_names()), len(CUES))


def _start_cues() -> None:
    with _lock:
        if _cue_state["started"] or len(_cues) == len(CUES):
            return
        _cue_state["started"] = True
    threading.Thread(target=_render_cues, name="vaani-cues", daemon=True).start()


def wav_ms(wav: bytes) -> int:
    """Length of a RIFF/WAVE clip in ms (data chunk size / byte rate); 0 when it cannot be read."""
    try:
        if len(wav) < 12 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return 0
        pos, byte_rate = 12, 0
        while pos + 8 <= len(wav):
            cid, size = wav[pos:pos + 4], int.from_bytes(wav[pos + 4:pos + 8], "little")
            if cid == b"fmt ":
                byte_rate = int.from_bytes(wav[pos + 16:pos + 20], "little")
            elif cid == b"data":
                size = min(size, len(wav) - pos - 8)
                return int(size * 1000 / byte_rate) if byte_rate else 0
            pos += 8 + size + (size & 1)
    except Exception:
        return 0
    return 0


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
