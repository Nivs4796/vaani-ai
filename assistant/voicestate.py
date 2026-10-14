"""Per-session voice state for voice mode: in memory only, never written to disk.

One VoiceState per chat session id holds what the approval rules need to know, decided
on the server and never taken from the page's word:
  mode         "voice" or "chat" (POST /voice/mode). Only voice mode accepts voice approvals.
  transcripts  issued by POST /voice/stt and /voice/wake only. Each has an id, the words, a
               confidence, when it was captured and whether it overlapped Vaani's own speech.
               take_transcript() hands one out once; typed text never becomes a transcript.
  speeches     every clip Vaani played for this session (read-backs, replies, cues), with the
               window it played in; a read-back records which card it read.
  arm          the 5-second countdown of a card that leaves the account ("send it").

Used by routes_voice.py (WAKE-STT), voice_policy.py and routes_voiceturn.py (POLICY) and
fastpath.py (PARSER, which may keep its own keys in VoiceState.data under "fp."). Times
are now() seconds (time.monotonic); tests patch voicestate.now. Contract: .build/CONTRACT-v3.md.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field

TRANSCRIPT_TTL_S = 15.0  # a transcript id is good for one use within this many seconds
READBACK_TTL_S = 30.0  # an approval must start within this many seconds of the read-back ending
COUNTDOWN_S = 5.0
COMMIT_EARLY_S = 0.15  # /voice/commit may arrive this much before the deadline (timer jitter)
COMMIT_GRACE_S = 10.0  # ... and no later than this after it, or the countdown has expired
PLAYBACK_LAG_S = 1.5  # a clip whose end was never reported is assumed to end this long after its audio
SPEECH_MARGIN_S = 0.25  # capture this close to Vaani's speech counts as overlapping it
KEEP = 32  # transcripts and speeches remembered per session

MODES = ("voice", "chat")


def now() -> float:
    return time.monotonic()


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(6)


@dataclass
class Transcript:
    tid: str
    session_id: str
    user_id: str | None
    text: str
    confidence: float
    issued_at: float
    capture_start: float
    capture_end: float
    overlap: bool  # the page said Vaani was playing, or the capture overlaps a recorded speech window
    purpose: str = "command"  # "command" (/voice/stt) | "wake_rest" (words after "Hey Vaani")
    used: bool = False

    def age(self, at: float | None = None) -> float:
        return (now() if at is None else at) - self.issued_at


@dataclass
class Speech:
    speak_id: str
    session_id: str
    kind: str  # "readback" | "say" | "cue"
    draft_id: str | None
    served_at: float
    audio_ms: int
    text: str = ""
    ended_at: float | None = None
    complete: bool | None = None  # True: played to the end; False: cut off (barge-in, Esc, stop); None: not reported

    def end_estimate(self) -> float:
        if self.ended_at is not None:
            return self.ended_at
        return self.served_at + self.audio_ms / 1000 + PLAYBACK_LAG_S

    def overlaps(self, start: float, end: float, margin: float = SPEECH_MARGIN_S) -> bool:
        return start < self.end_estimate() + margin and end > self.served_at - margin


@dataclass
class Arm:
    arm_id: str
    session_id: str
    draft_id: str
    tier: int
    word: str  # the action word that armed it, e.g. "send it"
    variant: str  # commit_pending variant ("" = the card's main action)
    armed_at: float
    deadline: float
    tid: str
    heard: str
    confidence: float
    state: str = "armed"  # armed | stopped | committed | expired
    reason: str = ""

    def remaining(self, at: float | None = None) -> float:
        return max(0.0, self.deadline - (now() if at is None else at))


@dataclass
class VoiceState:
    session_id: str
    user_id: str | None = None
    mode: str = "chat"
    transcripts: deque = field(default_factory=lambda: deque(maxlen=KEEP))
    speeches: deque = field(default_factory=lambda: deque(maxlen=KEEP))
    arm: Arm | None = None
    stt_inflight: int = 0
    last_stop_at: float | None = None
    data: dict = field(default_factory=dict)  # free space: "fp.*" keys belong to fastpath.py, "pol.*" to voice_policy.py


_states: dict[str, VoiceState] = {}
_lock = threading.RLock()


def get(session_id: str, user_id: str | None = None) -> VoiceState:
    """The session's state, created in chat mode on first use."""
    sid = str(session_id)
    with _lock:
        st = _states.get(sid)
        if st is None:
            st = _states[sid] = VoiceState(session_id=sid, user_id=user_id)
        elif user_id is not None and st.user_id is None:
            st.user_id = user_id
        return st


def peek(session_id: str) -> VoiceState | None:
    with _lock:
        return _states.get(str(session_id))


def drop(session_id: str) -> None:
    with _lock:
        _states.pop(str(session_id), None)


def reset_all() -> None:
    """Tests only."""
    with _lock:
        _states.clear()


# --------------------------------------------------------------------------- mode

def set_mode(session_id: str, mode: str, user_id: str | None = None) -> VoiceState:
    """Switch the session to "voice" or "chat". Leaving voice mode stops any countdown."""
    if mode not in MODES:
        raise ValueError(f"mode must be voice or chat, not {mode!r}")
    with _lock:
        st = get(session_id, user_id)
        st.mode = mode
        if mode != "voice":
            _disarm(st, "switched to chat")
        return st


def is_voice(session_id: str) -> bool:
    st = peek(session_id)
    return bool(st and st.mode == "voice")


# --------------------------------------------------------------------------- Vaani's own speech

def start_speech(session_id: str, kind: str, audio_ms: int, draft_id: str | None = None, text: str = "") -> Speech:
    """Record a clip Vaani is about to play (called when /voice/tts or /voice/cue serves it)."""
    with _lock:
        st = get(session_id)
        sp = Speech(speak_id=_id("s"), session_id=st.session_id, kind=kind, draft_id=draft_id, served_at=now(),
                    audio_ms=max(0, int(audio_ms or 0)), text=text)
        st.speeches.append(sp)
        return sp


def end_speech(session_id: str, speak_id: str, complete: bool) -> Speech | None:
    """The page reports a clip ended: played to the end (complete) or cut off. First report wins."""
    with _lock:
        st = peek(session_id)
        if st is None:
            return None
        for sp in st.speeches:
            if sp.speak_id == speak_id:
                if sp.ended_at is None:
                    sp.ended_at = now()
                    sp.complete = bool(complete)
                return sp
        return None


def speaking_overlaps(session_id: str, start: float, end: float) -> bool:
    """True when [start, end] overlaps any clip Vaani played (or may still be playing) for this session."""
    with _lock:
        st = peek(session_id)
        return bool(st) and any(sp.overlaps(start, end) for sp in st.speeches)


def last_readback(session_id: str, draft_id: str) -> Speech | None:
    """The newest read-back of this card, or None."""
    with _lock:
        st = peek(session_id)
        if st is None:
            return None
        for sp in reversed(st.speeches):
            if sp.kind == "readback" and sp.draft_id == str(draft_id):
                return sp
        return None


# --------------------------------------------------------------------------- transcripts

@contextmanager
def stt_running(session_id: str):
    """Wrap a transcription: while one runs, a countdown commit is held (a "stop" may be in it)."""
    with _lock:
        get(session_id).stt_inflight += 1
    try:
        yield
    finally:
        with _lock:
            st = get(session_id)
            st.stt_inflight = max(0, st.stt_inflight - 1)


def issue_transcript(session_id: str, user_id: str | None, text: str, confidence: float, capture_start: float,
                     capture_end: float, client_overlap: bool = False, purpose: str = "command") -> Transcript:
    """Store what the Mac heard and return it with a fresh single-use id. Only /voice/stt and /voice/wake call this."""
    with _lock:
        st = get(session_id, user_id)
        overlap = bool(client_overlap) or speaking_overlaps(session_id, capture_start, capture_end)
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            conf = 0.0
        t = Transcript(tid=_id("t"), session_id=st.session_id, user_id=user_id, text=str(text or "").strip(),
                       confidence=conf, issued_at=now(), capture_start=float(capture_start),
                       capture_end=float(capture_end), overlap=overlap, purpose=purpose)
        st.transcripts.append(t)
        return t


def take_transcript(session_id: str, user_id: str | None, tid: str) -> Transcript | None:
    """Hand out a transcript once. None when the id is unknown, from another user or session,
    already used, or older than TRANSCRIPT_TTL_S."""
    with _lock:
        st = peek(session_id)
        if st is None or not tid:
            return None
        for t in st.transcripts:
            if t.tid != str(tid):
                continue
            if t.used or t.user_id != user_id or t.session_id != st.session_id:
                return None
            t.used = True  # single use, whatever the caller then decides
            if t.age() > TRANSCRIPT_TTL_S:
                return None
            return t
        return None


# --------------------------------------------------------------------------- countdown

def arm(session_id: str, draft_id: str, tier: int, word: str, transcript: Transcript, variant: str = "",
        seconds: float = COUNTDOWN_S) -> Arm:
    """Start a countdown for a card that leaves the account. Replaces (stops) any earlier one."""
    with _lock:
        st = get(session_id)
        _disarm(st, "replaced by a newer countdown")
        t0 = now()
        st.arm = Arm(arm_id=_id("a"), session_id=st.session_id, draft_id=str(draft_id), tier=int(tier), word=word,
                     variant=variant or "", armed_at=t0, deadline=t0 + seconds, tid=transcript.tid,
                     heard=transcript.text, confidence=transcript.confidence)
        return st.arm


def current_arm(session_id: str) -> Arm | None:
    """The countdown still running, or None (expired ones are marked expired here)."""
    with _lock:
        st = peek(session_id)
        a = st.arm if st else None
        if a is None or a.state != "armed":
            return None
        if now() > a.deadline + COMMIT_GRACE_S:
            a.state, a.reason = "expired", "the countdown ran out without a commit"
            return None
        return a


def _disarm(st: VoiceState, reason: str) -> Arm | None:
    a = st.arm
    if a is not None and a.state == "armed":
        a.state, a.reason = "stopped", reason
        return a
    return None


def disarm(session_id: str, reason: str = "stopped") -> Arm | None:
    """Stop the countdown (stop word, Esc, tap, switching to chat). Returns the stopped arm or None."""
    with _lock:
        st = peek(session_id)
        return _disarm(st, reason) if st else None


def note_stop(session_id: str, reason: str = "stop word") -> Arm | None:
    """A stop or cancel arrived from any source: remember when, and stop any countdown."""
    with _lock:
        st = get(session_id)
        st.last_stop_at = now()
        return _disarm(st, reason)


def mark_committed(session_id: str, arm_id: str) -> bool:
    """Claim an armed countdown for its one commit. False if it is not that arm, or not armed."""
    with _lock:
        st = peek(session_id)
        a = st.arm if st else None
        if a is None or a.arm_id != str(arm_id) or a.state != "armed":
            return False
        a.state = "committed"
        return True
