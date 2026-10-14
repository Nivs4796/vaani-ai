"""Offline checks for assistant/voicestate.py: the per-session voice state the approval rules trust.

Mode flag, single-use transcripts with a time limit, Vaani's own speech windows (overlap and
read-backs), the countdown (arm, stop, commit once, expiry) and the stt-in-flight hold.
No model, no Google, no audio. The clock is patched (voicestate.now).
Run: .venv/bin/python -m evals.test_voicestate
"""

from __future__ import annotations

import sys

from assistant import voicestate as vs

RESULTS: list[tuple[str, bool, str]] = []
CLOCK = [1000.0]


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    if not ok:
        print(f"FAIL  {name}\n      {str(detail)[:400]}")


def tick(s: float) -> None:
    CLOCK[0] += s


def setup() -> None:
    vs.reset_all()
    CLOCK[0] = 1000.0
    vs.now = lambda: CLOCK[0]


def heard(sid="s1", uid="u1", text="confirm", conf=0.9, start=None, end=None, overlap=False):
    end = CLOCK[0] if end is None else end
    start = end - 0.8 if start is None else start
    return vs.issue_transcript(sid, uid, text, conf, start, end, client_overlap=overlap)


def check_mode():
    setup()
    check("a new session starts in chat mode", vs.get("s1").mode == "chat" and not vs.is_voice("s1"))
    check("unknown session is not voice", not vs.is_voice("nope"))
    vs.set_mode("s1", "voice", "u1")
    check("set_mode voice", vs.is_voice("s1") and vs.get("s1").user_id == "u1")
    try:
        vs.set_mode("s1", "shout")
        check("bad mode refused", False)
    except ValueError:
        check("bad mode refused", True)
    t = heard()
    a = vs.arm("s1", "d1", 2, "send it", t)
    vs.set_mode("s1", "chat")
    check("switching to chat stops the countdown", a.state == "stopped" and vs.current_arm("s1") is None, a)
    check("chat mode after switch", not vs.is_voice("s1"))


def check_transcripts():
    setup()
    vs.set_mode("s1", "voice", "u1")
    t = heard(text="  Confirm.  ", conf=1.7)
    check("transcript gets an id", t.tid.startswith("t") and len(t.tid) > 6, t.tid)
    check("text trimmed, confidence clamped", t.text == "Confirm." and t.confidence == 1.0, (t.text, t.confidence))
    bad = vs.issue_transcript("s1", "u1", "x", "not a number", 0, 1)
    check("non-numeric confidence becomes 0", bad.confidence == 0.0)
    check("another user cannot take it", vs.take_transcript("s1", "u2", t.tid) is None)
    t2 = heard()
    check("another session cannot take it", vs.take_transcript("s2", "u1", t2.tid) is None)
    got = vs.take_transcript("s1", "u1", t2.tid)
    check("owner takes it once", got is t2 and got.used)
    check("second take refused (single use)", vs.take_transcript("s1", "u1", t2.tid) is None)
    check("unknown id refused", vs.take_transcript("s1", "u1", "t000000") is None)
    check("empty id refused", vs.take_transcript("s1", "u1", "") is None)
    t3 = heard()
    tick(vs.TRANSCRIPT_TTL_S + 0.1)
    check("stale transcript refused", vs.take_transcript("s1", "u1", t3.tid) is None)
    check("stale transcript is burnt too", t3.used)
    t4 = heard()
    tick(vs.TRANSCRIPT_TTL_S - 0.5)
    check("fresh-enough transcript accepted", vs.take_transcript("s1", "u1", t4.tid) is t4)
    t5 = vs.issue_transcript("s1", None, "confirm", 0.9, CLOCK[0] - 1, CLOCK[0])
    check("single mode (user None) takes its own", vs.take_transcript("s1", None, t5.tid) is t5)
    t6 = vs.issue_transcript("s1", None, "confirm", 0.9, CLOCK[0] - 1, CLOCK[0])
    check("a signed-in user cannot take a user-less transcript", vs.take_transcript("s1", "u1", t6.tid) is None)
    for _ in range(vs.KEEP + 5):
        heard()
    check("transcripts are capped per session", len(vs.get("s1").transcripts) == vs.KEEP)


def check_speech():
    setup()
    vs.set_mode("s1", "voice", "u1")
    sp = vs.start_speech("s1", "readback", 3000, draft_id="d1", text="Here's the reply")
    check("speech recorded with an id", sp.speak_id.startswith("s") and sp.served_at == CLOCK[0])
    check("unreported end is estimated with lag", abs(sp.end_estimate() - (CLOCK[0] + 3.0 + vs.PLAYBACK_LAG_S)) < 1e-9)
    t = heard(start=CLOCK[0] + 1.0, end=CLOCK[0] + 2.0)
    check("capture during playback overlaps", t.overlap)
    t = heard(start=CLOCK[0] - 5, end=CLOCK[0] - 2)
    check("capture well before playback does not overlap", not t.overlap)
    t = heard(start=CLOCK[0] - 5, end=CLOCK[0] - 0.1)
    check("capture ending within the margin before playback overlaps", t.overlap)
    tick(2.0)
    ended = vs.end_speech("s1", sp.speak_id, complete=True)
    check("end_speech records the end", ended is sp and sp.ended_at == CLOCK[0] and sp.complete is True)
    vs.end_speech("s1", sp.speak_id, complete=False)
    check("first end report wins", sp.complete is True and sp.ended_at == CLOCK[0])
    t = heard(start=CLOCK[0] + 0.3, end=CLOCK[0] + 1.2)
    check("capture after the reported end does not overlap", not t.overlap, t)
    t = heard(start=CLOCK[0] + 0.1, end=CLOCK[0] + 1.0)
    check("capture inside the margin after the end overlaps", t.overlap)
    check("client overlap flag always wins", heard(start=CLOCK[0] + 50, end=CLOCK[0] + 51, overlap=True).overlap)
    check("last_readback finds the read-back of that card", vs.last_readback("s1", "d1") is sp)
    check("last_readback ignores other cards", vs.last_readback("s1", "d2") is None)
    vs.start_speech("s1", "say", 500)
    check("a later non-readback clip does not replace the read-back", vs.last_readback("s1", "d1") is sp)
    sp2 = vs.start_speech("s1", "readback", 1000, draft_id="d1")
    check("the newest read-back of a card wins", vs.last_readback("s1", "d1") is sp2)
    check("unknown speak id ends nothing", vs.end_speech("s1", "s000", True) is None)
    check("unknown session ends nothing", vs.end_speech("zz", sp.speak_id, True) is None)
    check("unknown session has no overlap", not vs.speaking_overlaps("zz", 0, 1e9))


def check_countdown():
    setup()
    vs.set_mode("s1", "voice", "u1")
    t = heard(text="send it")
    a = vs.arm("s1", "d1", 2, "send it", t)
    check("arm sets a deadline", a.state == "armed" and abs(a.deadline - (CLOCK[0] + vs.COUNTDOWN_S)) < 1e-9)
    check("arm keeps who said what", a.heard == "send it" and a.tid == t.tid and a.confidence == t.confidence)
    check("current_arm returns it", vs.current_arm("s1") is a)
    tick(2)
    check("remaining counts down", abs(a.remaining() - 3) < 1e-9)
    b = vs.arm("s1", "d1", 2, "send it", heard(text="send it"))
    check("a new arm stops the old one", a.state == "stopped" and vs.current_arm("s1") is b)
    check("commit claims the arm once", vs.mark_committed("s1", b.arm_id) and b.state == "committed")
    check("second commit refused", not vs.mark_committed("s1", b.arm_id))
    check("committed arm is no longer current", vs.current_arm("s1") is None)
    c = vs.arm("s1", "d2", 2, "trash it", heard(text="trash it"))
    stopped = vs.disarm("s1", "esc")
    check("disarm stops it", stopped is c and c.state == "stopped" and c.reason == "esc")
    check("stopped arm cannot commit", not vs.mark_committed("s1", c.arm_id))
    check("disarm with nothing armed returns None", vs.disarm("s1") is None)
    d = vs.arm("s1", "d3", 2, "send it", heard(text="send it"))
    vs.note_stop("s1")
    check("a stop word stops the countdown and is remembered", d.state == "stopped" and vs.get("s1").last_stop_at == CLOCK[0])
    e = vs.arm("s1", "d4", 2, "send it", heard(text="send it"))
    check("wrong arm id cannot commit", not vs.mark_committed("s1", "a000000") and e.state == "armed")
    tick(vs.COUNTDOWN_S + vs.COMMIT_GRACE_S + 0.1)
    check("an arm past its grace expires", vs.current_arm("s1") is None and e.state == "expired")
    check("expired arm cannot commit", not vs.mark_committed("s1", e.arm_id))
    check("unknown session has no arm", vs.current_arm("zz") is None and vs.disarm("zz") is None)


def check_stt_hold():
    setup()
    st = vs.get("s1")
    with vs.stt_running("s1"):
        check("stt in flight is counted", st.stt_inflight == 1)
        with vs.stt_running("s1"):
            check("nested transcriptions count", st.stt_inflight == 2)
    check("count returns to zero", st.stt_inflight == 0)
    try:
        with vs.stt_running("s1"):
            raise RuntimeError("decode failed")
    except RuntimeError:
        pass
    check("count returns to zero after an error", st.stt_inflight == 0)


def check_isolation():
    setup()
    vs.set_mode("s1", "voice", "u1")
    vs.set_mode("s2", "voice", "u2")
    t = heard("s1", "u1")
    vs.start_speech("s2", "readback", 5000, draft_id="d1")
    check("another session's speech is not an overlap here", not heard("s1", "u1").overlap)
    check("another session cannot see this read-back", vs.last_readback("s1", "d1") is None)
    vs.arm("s2", "d1", 2, "send it", heard("s2", "u2", "send it"))
    check("another session's countdown is separate", vs.current_arm("s1") is None and vs.current_arm("s2") is not None)
    vs.drop("s2")
    check("drop forgets a session", vs.peek("s2") is None and vs.peek("s1") is not None)
    check("s1 transcript still takeable after s2 dropped", vs.take_transcript("s1", "u1", t.tid) is t)


def main() -> int:
    real_now = vs.now
    try:
        check_mode()
        check_transcripts()
        check_speech()
        check_countdown()
        check_stt_hold()
        check_isolation()
    finally:
        vs.now = real_now
        vs.reset_all()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} voicestate checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
