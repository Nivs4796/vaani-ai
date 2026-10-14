"""Offline checks for voice approvals (assistant/voice_policy.py) and the pieces of manage.py
and autonomy.py they rely on: the tier table, the read-back text, the seven approval checks
with their fixed spoken reasons, the saved-draft send card (Gmail drafts.send), the
VAANI_BLOCK_SEND switch, and voice approvals in Proof and the promotion streak.

No model, no network, no real mailbox: the fake Gmail and Calendar from evals.test_manage,
a temp log, temp memory and a patched voicestate clock. Made-up people only.
Run: .venv/bin/python -m evals.test_voice_policy   (exit 1 on any failure)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from assistant import agent, autonomy, brief, flow, llm, manage, obs, voice, voicecmd, voicestate
from assistant import voice_policy as vp
from evals import test_manage as tm

RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    if not ok:
        print(f"FAIL  {name}\n      {str(detail)[:500]}")


# --------------------------------------------------------------------------- fakes and helpers


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


CLOCK = Clock()


def install_fakes():
    """A fake Gmail drafts.send (test_manage's fake has none), the clock, no model, no brief cache."""

    def drafts_send(self, userId="me", body=None):
        def run():
            self.g.n += 1
            mid = f"sent{self.g.n}"
            self.g.sent_raw.append({"draft_send": (body or {}).get("id")})
            self.g.msgs[mid] = {"id": mid, "threadId": mid, "labelIds": ["SENT"], "snippet": "",
                                "payload": {"headers": [], "mimeType": "text/plain", "body": {}}}
            return {"id": mid, "threadId": mid, "labelIds": ["SENT"]}
        return tm.Req(run)

    tm._Drafts.send = drafts_send
    voicestate.now = CLOCK
    brief.invalidate = lambda user_id=None: None

    def no_model(*a, **k):
        raise AssertionError("the model must not be called in these checks")

    llm.complete = no_model


def fresh_log() -> Path:
    path = Path(tempfile.mkdtemp()) / "tools.jsonl"
    obs.TOOLS_LOG = path
    return path


def rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def writes(g, c) -> tuple:
    """Everything a write could change in the fakes."""
    return (len(g.sent_raw), len(g.drafts_made), json.dumps({k: v["labelIds"] for k, v in g.msgs.items()}, sort_keys=True),
            len(c.inserted), len(c.patches), json.dumps({k: v.get("status") for k, v in c.ev.items()}, sort_keys=True))


def in_session(session, fn, *a, **k):
    tok = obs.current_session.set(session)
    try:
        return fn(*a, **k)
    finally:
        obs.current_session.reset(tok)


def reply_card(session) -> dict:
    tm.user(session, "Reply to Ravi saying Thursday at 4pm works for me")
    r = tm.j(in_session(session, manage.draft_email, None, "", "Hi Ravi,\nThursday at 4pm works for me.\nAlex",
                        reply_to_message_id="m1"))
    return session.drafts[r["draft_id"]]


def read_back(sid: str, did: str, complete: bool = True, secs: float = 3.0):
    sp = voicestate.start_speech(sid, "readback", int(secs * 1000), draft_id=did, text="read-back")
    CLOCK.t += secs
    voicestate.end_speech(sid, sp.speak_id, complete)
    return sp


def heard(sid: str, text: str, conf: float = 0.92, gap: float = 1.0, overlap: bool = False, user_id=None):
    """A transcript as /voice/stt would issue it, captured `gap` seconds after now."""
    CLOCK.t += gap
    t = voicestate.issue_transcript(sid, user_id, text, conf, CLOCK.t, CLOCK.t + 0.8, client_overlap=overlap)
    CLOCK.t += 0.9
    return voicestate.take_transcript(sid, user_id, t.tid)


def voice_session():
    s = agent.new_session()
    voicestate.set_mode(s.id, "voice")
    return s


# --------------------------------------------------------------------------- checks


def check_conf_min():
    os.environ.pop("VAANI_VOICE_CONF_MIN", None)
    check("conf_min defaults to 0.60", vp.conf_min() == 0.60, vp.conf_min())
    os.environ["VAANI_VOICE_CONF_MIN"] = "0.75"
    check("conf_min is read at call time", vp.conf_min() == 0.75, vp.conf_min())
    os.environ["VAANI_VOICE_CONF_MIN"] = "loud"
    check("a bad conf_min falls back to 0.60", vp.conf_min() == 0.60, vp.conf_min())
    os.environ.pop("VAANI_VOICE_CONF_MIN", None)


def check_table():
    def words(d):
        return [(a["word"], a["tier"], a["variant"]) for a in vp.approvals(d)]

    email = {"kind": "email_send", "to": ["sam.lee@example.org"]}
    check("email: confirm saves to drafts (tier 1), send it sends (tier 2)",
          words(email) == [("confirm", 1, "save_draft"), ("send it", 2, "")], words(email))
    saved = {"kind": "email_send", "gmail_draft_id": "r1"}
    check("saved-draft send card: only send it (tier 2)", words(saved) == [("send it", 2, "")], words(saved))
    for act in ("archive", "unarchive", "mark_read", "mark_unread", "star", "unstar", "label", "unlabel", "untrash"):
        if words({"kind": "email_modify", "action": act}) != [("confirm", 1, "")]:
            check(f"{act} is tier 1 confirm", False, words({"kind": "email_modify", "action": act}))
            break
    else:
        check("every reversible mailbox change is tier 1 confirm", True)
    check("trash is tier 2: trash it or delete it",
          words({"kind": "email_modify", "action": "trash"}) == [("trash it", 2, ""), ("delete it", 2, "")])
    check("new event without guests: confirm (tier 1)", words({"kind": "event_create"}) == [("confirm", 1, "")])
    check("new event with guests: invite them (tier 2)", words({"kind": "event_create", "attendees": ["sam.lee@example.org"]}) == [("invite them", 2, "")])
    from assistant import tools as _tools
    real_own = _tools._own_address
    _tools._own_address = lambda: "alex@example.org"
    try:
        check("the owner alone is not a guest: confirm (tier 1)",
              words({"kind": "event_create", "attendees": ["Alex@Example.org"]}) == [("confirm", 1, "")])
        check("owner plus someone else is still a guest event (tier 2)",
              words({"kind": "event_delete", "attendees": ["alex@example.org", "sam.lee@example.org"]}) == [("delete it", 2, "")])
        check("the read-back of an owner-only event says no guests",
              "no guests" in vp.readback_text({"kind": "event_create", "draft_id": "d1", "summary": "Focus time",
                                               "start": "2026-10-15T10:00:00+05:30", "end": "2026-10-15T10:30:00+05:30",
                                               "attendees": ["alex@example.org"]}))
    finally:
        _tools._own_address = real_own
    check("move without guests: confirm; with guests: move it",
          words({"kind": "event_update"}) == [("confirm", 1, "")] and words({"kind": "event_update", "attendees": ["a@example.org"]}) == [("move it", 2, "")])
    check("delete without guests: confirm; with guests: delete it",
          words({"kind": "event_delete"}) == [("confirm", 1, "")] and words({"kind": "event_delete", "attendees": ["a@example.org"]}) == [("delete it", 2, "")])
    check("RSVP words match the card's answer (tier 2)",
          [words({"kind": "event_rsvp", "response": r}) for r in ("accepted", "declined", "tentative")]
          == [[("accept it", 2, "")], [("decline it", 2, "")], [("say maybe", 2, "")]])
    check("say words for an email card", vp.say_words(email) == ["confirm", "send it", "change it", "cancel"], vp.say_words(email))
    check("say words for the saved-draft send card", vp.say_words(saved) == ["send it", "not now"], vp.say_words(saved))
    t = vp.tier_of({"kind": "email_modify", "action": "trash"})
    check("tier_of keeps its shape", set(t) == {"tier", "word", "words", "variant"} and t["tier"] == 2 and t["word"] == "trash it", t)
    check("save it is the confirm family", vp.word_of(voicecmd.parse("save it")) == "confirm")


def check_readback(g, c):
    s = voice_session()
    d = reply_card(s)
    text = vp.readback_text(d)
    want = "Here's the reply to Ravi: Thursday at 4pm works for me. Say confirm to save it to your drafts, or send it to send it."
    check("reply read-back is built in code from the card", text == want, text)
    tm.user(s, "Email sam.lee@example.org about the plan")
    long_body = ("Hi Sam,\nSee https://example.org/plan?id=abc123 and write to sam.lee@example.org. "
                 + "The plan has many parts and every part matters a great deal to the team. " * 8 + "\nBest,\nAlex")
    r = tm.j(in_session(s, manage.draft_email, ["sam.lee@example.org"], "The plan", long_body))
    t2 = vp.readback_text(s.drafts[r["draft_id"]])
    check("no URLs or email addresses are read aloud", "http" not in t2 and "@" not in t2 and "example.org" not in t2, t2)
    check("a long body is capped (~300 characters)", len(t2) < 420 and "…" in t2, len(t2))
    check("a new email names the recipient from the address", t2.startswith("Here's the email to Sam:"), t2[:60])
    tr = {"kind": "email_modify", "action": "trash", "ids": ["m2", "m3"],
          "card": {"lines": ["Cloudflare - Connect is almost sold out", "Atlassian - Tell us"]}}
    t3 = vp.readback_text(tr)
    check("trash read-back: verb, count, first sender, the word and the countdown",
          t3 == "Move 2 emails to Trash, starting with Cloudflare. Say trash it, then I'll count down from five.", t3)
    arch = dict(tr, action="archive", ids=["m3"], card={"lines": ["Atlassian - Tell us"]})
    t4 = vp.readback_text(arch)
    check("archive read-back says confirm", t4 == "Archive 1 email, from Atlassian. Say confirm to go ahead.", t4)
    ev = {"kind": "event_create", "summary": "Design sync", "start": "2026-10-15T10:00:00+05:30", "end": "2026-10-15T10:30:00+05:30", "attendees": []}
    t5 = vp.readback_text(ev)
    check("event read-back: title, when, no guests", t5.startswith("New event: Design sync, Thursday 15 October at 10:00, no guests.")
          and t5.endswith("Say confirm to go ahead."), t5)
    t6 = vp.readback_text(dict(ev, attendees=["sam.lee@example.org"]))
    check("event with guests names them and asks for invite them", "with Sam Lee" in t6 and t6.endswith("Say invite them, then I'll count down from five."), t6)
    saved = {"kind": "email_send", "gmail_draft_id": "r1", "to": ["sam.lee@example.org"]}
    check("saved-draft send card read-back", vp.readback_text(saved) == vp.SAVED_LINE, vp.readback_text(saved))
    rs = {"kind": "event_rsvp", "response": "declined", "summary": "Design review", "when_iso": "2026-10-15T14:00:00+05:30"}
    check("RSVP read-back asks for decline it", vp.readback_text(rs).endswith("Say decline it, then I'll count down from five."), vp.readback_text(rs))
    fa = vp.focus_approval(d)
    check("email focus card matches the mock-up", fa["label"] == "AWAITING VOICE APPROVAL" and fa["title"] == "Reply to Ravi Mehta"
          and fa["say"] == ["confirm", "send it", "change it", "cancel"] and fa["fine"] == vp.FINE_SAVE, fa)
    check("countdown line names the recipient", vp.countdown_line(d) == "Sending to Ravi in five seconds. Say stop to cancel.", vp.countdown_line(d))


def check_checks(g, c):
    s = voice_session()
    sid = s.id
    d = reply_card(s)
    did = d["draft_id"]
    before = writes(g, c)
    confirm = voicecmd.parse("confirm")

    def verdict(t, cmd=confirm, sess=s):
        return vp.check(sess, sid, t, cmd)

    read_back(sid, did)
    v = verdict(heard(sid, "confirm"))
    check("all seven checks pass: tier 1 save_draft", v.ok and v.approval == {"word": "confirm", "tier": 1, "variant": "save_draft"}, v)

    voicestate.set_mode(sid, "chat")
    read_back(sid, did)
    t = heard(sid, "confirm")
    v = verdict(t)
    check("1. chat mode: refused", not v.ok and v.code == "mode" and v.spoken == vp.REASONS["mode"], v)
    voicestate.set_mode(sid, "voice")

    v = verdict(None)
    check("2. no fresh transcript: refused with the fixed reason", not v.ok and v.code == "stale" and v.spoken == vp.REASONS["stale"], v)
    tt = voicestate.issue_transcript(sid, None, "confirm", 0.9, CLOCK.t, CLOCK.t + 0.5)
    first = voicestate.take_transcript(sid, None, tt.tid)
    check("2. a transcript id is single use", first is not None and voicestate.take_transcript(sid, None, tt.tid) is None)
    tt = voicestate.issue_transcript(sid, None, "confirm", 0.9, CLOCK.t, CLOCK.t + 0.5)
    CLOCK.t += voicestate.TRANSCRIPT_TTL_S + 1
    check("2. a transcript older than 15 s is refused", voicestate.take_transcript(sid, None, tt.tid) is None)
    tt = voicestate.issue_transcript(sid, "someone-else", "confirm", 0.9, CLOCK.t, CLOCK.t + 0.5)
    check("2. another user's transcript is refused", voicestate.take_transcript(sid, None, tt.tid) is None)

    read_back(sid, did)
    v = verdict(heard(sid, "confirm", overlap=True))
    check("3. overlap flag from the page: refused", not v.ok and v.code == "overlap", v)
    sp = voicestate.start_speech(sid, "readback", 4000, draft_id=did)
    tt = voicestate.issue_transcript(sid, None, "confirm", 0.95, CLOCK.t + 1, CLOCK.t + 2)  # while Vaani is speaking
    voicestate.end_speech(sid, sp.speak_id, True)
    v = verdict(voicestate.take_transcript(sid, None, tt.tid))
    check("3. a capture overlapping Vaani's own read-back is refused", not v.ok and v.code == "overlap", v)

    read_back(sid, did)
    v = verdict(heard(sid, "confirm", conf=0.41))
    check("4. low confidence: refused", not v.ok and v.code == "low_conf" and v.spoken == vp.REASONS["low_conf"], v)

    read_back(sid, did)
    v = verdict(heard(sid, "confirm the time with Ravi"), voicecmd.Command(intent="confirm", word="confirm", exact=False))
    check("5. not the whole utterance: refused", not v.ok and v.code == "not_exact", v)
    check("5. don't confirm is never an approval", voicecmd.parse("don't confirm").intent not in ("confirm", "action"))

    read_back(sid, did)
    v = verdict(heard(sid, "trash it"), voicecmd.parse("trash it"))
    check("6. a word that is not this card's: refused, says the right words",
          not v.ok and v.code == "wrong_word" and "confirm" in v.spoken and "send it" in v.spoken, v)

    s2 = voice_session()
    v = vp.check(s2, s2.id, heard(s2.id, "confirm"), confirm)
    check("no card: nothing waiting", not v.ok and v.code == "no_card" and v.spoken == vp.NOTHING_WAITING, v)

    s3 = voice_session()
    d3 = reply_card(s3)
    v = vp.check(s3, s3.id, heard(s3.id, "confirm"), confirm)
    check("7. no read-back: Vaani reads THIS card back", not v.ok and v.code == "readback" and v.spoken == vp.readback_text(d3), v)
    read_back(s3.id, d3["draft_id"], complete=False)
    v = vp.check(s3, s3.id, heard(s3.id, "confirm"), confirm)
    check("7. an interrupted read-back does not count", not v.ok and v.code == "readback", v)
    read_back(s3.id, d3["draft_id"])
    CLOCK.t += voicestate.READBACK_TTL_S + 1
    v = vp.check(s3, s3.id, heard(s3.id, "confirm"), confirm)
    check("7. a read-back older than 30 s does not count", not v.ok and v.code == "readback", v)
    read_back(s3.id, d3["draft_id"])
    d4 = reply_card(s3)  # read back A, then B is pending
    v = vp.check(s3, s3.id, heard(s3.id, "confirm"), confirm)
    check("7. a different card is pending: read THAT card back", not v.ok and v.code == "readback" and v.draft["draft_id"] == d4["draft_id"], v)
    read_back(s3.id, d4["draft_id"])
    voicestate.note_stop(s3.id)
    v = vp.check(s3, s3.id, heard(s3.id, "confirm"), confirm)
    check("7. a stop after the read-back needs a fresh read-back", not v.ok and v.code == "readback", v)
    check("no refusal wrote anything", writes(g, c) == before)


def check_manage(g, c):
    log = fresh_log()
    s = voice_session()
    d = reply_card(s)
    did = d["draft_id"]
    out = flow.confirm(s, did, "save_draft", confirmer="voice")
    check("save_draft by voice is verified", voice.receipt_ok(out.get("receipt")) and len(g.drafts_made) == 1, out.get("receipt"))
    card = in_session(s, manage.draft_send_saved, s.drafts[did])
    check("draft_send_saved makes a pending send card for THAT saved draft",
          card and s.pending_draft_id == card["draft_id"] and card["kind"] == "email_send"
          and card["gmail_draft_id"] == s.drafts[did]["committed"]["gmail_draft_id"] and card["card"]["title"] == "Send the saved draft"
          and card["card"]["confirm_label"] == "Send" and card["card"]["alt"] is None and card["origin"] == d["origin"]
          and card["to"] == d["to"] and card["body"] == d["body"] and card["thread_id"] == d["thread_id"], card)
    check("draft_send_saved is not a model tool", "draft_send_saved" not in manage.REGISTRY
          and all(t["function"]["name"] != "draft_send_saved" for t in manage.TOOL_SCHEMAS))
    check("no saved-draft card from an unverified save", manage.draft_send_saved({"committed": {"status": "SAVED_DRAFT", "verified": False,
                                                                                               "gmail_draft_id": "r9"}}, s) is None)
    sent_before = len(g.sent_raw)
    out = flow.confirm(s, card["draft_id"], "save_draft")
    check("save_draft is refused on the saved-draft card", len(g.sent_raw) == sent_before and len(g.drafts_made) == 1
          and not voice.receipt_ok(out.get("receipt")), out.get("reply"))
    s5 = voice_session()
    d5 = reply_card(s5)
    flow.confirm(s5, d5["draft_id"], "save_draft")
    card5 = in_session(s5, manage.draft_send_saved, s5.drafts[d5["draft_id"]])
    os.environ["VAANI_BLOCK_SEND"] = "1"
    try:
        n0 = len(g.sent_raw)
        out = flow.confirm(s5, card5["draft_id"], None, confirmer="voice")
        refused = "sending is switched off on this server (VAANI_BLOCK_SEND=1); nothing was sent" in (out.get("reply") or "")
        check("VAANI_BLOCK_SEND=1 refuses the send, card stays pending", refused and len(g.sent_raw) == n0
              and s5.pending_draft_id == card5["draft_id"] and s5.drafts[card5["draft_id"]]["status"] == "DRAFT", out.get("reply"))
        s6 = voice_session()
        d6 = reply_card(s6)
        out6 = flow.confirm(s6, d6["draft_id"], "save_draft")
        check("VAANI_BLOCK_SEND=1 still saves to drafts", voice.receipt_ok(out6.get("receipt")), out6.get("reply"))
        out7 = flow.confirm(s6, s6.drafts[d6["draft_id"]]["draft_id"], None)
        check("VAANI_BLOCK_SEND=1 also refuses a clicked send", len(g.sent_raw) == n0, out7.get("reply"))
    finally:
        os.environ.pop("VAANI_BLOCK_SEND", None)
    out = flow.confirm(s5, card5["draft_id"], None, confirmer="voice")
    sends = [x for x in g.sent_raw if x.get("draft_send")]
    check("the saved-draft card sends via drafts.send, once, verified in Sent",
          len(sends) == 1 and sends[0]["draft_send"] == card5["gmail_draft_id"] and voice.receipt_ok(out.get("receipt"))
          and out["receipt"]["status"] == "SENT", (sends, out.get("receipt")))
    again = flow.confirm(s5, card5["draft_id"], None, confirmer="voice")
    check("and never twice", len([x for x in g.sent_raw if x.get("draft_send")]) == 1, again.get("reply"))

    commits = [r for r in rows(log) if r.get("tool") == "commit_pending" and r.get("card_kind")]
    vrows = [r for r in commits if r.get("confirmed_by") == "voice"]
    check("voice commit rows carry heard and stt_conf only when set", vrows and all("heard" in r and "stt_conf" in r for r in vrows)
          and all("heard" not in r for r in commits if r.get("confirmed_by") != "voice"), vrows[:1])

    tok = obs.current_voice.set({"heard": "confirm " + "x" * 120, "confidence": 0.876543, "tid": "t1"})
    try:
        s7 = voice_session()
        d7 = reply_card(s7)
        flow.confirm(s7, d7["draft_id"], "save_draft", confirmer="voice")
    finally:
        obs.current_voice.reset(tok)
    row = [r for r in rows(log) if r.get("draft_id") == d7["draft_id"] and r.get("card_kind")][-1]
    check("heard is capped at 80 characters, stt_conf has 2 decimals", len(row["heard"]) == 80 and row["stt_conf"] == 0.88
          and row["confirmed_by"] == "voice", row)

    s8 = voice_session()
    tm.user(s8, "delete the call with Andy Chen tomorrow")
    r = tm.j(in_session(s8, manage.draft_event_change, "delete", event_id="e1"))
    check("event_delete drafts store the guests", s8.drafts[r["draft_id"]].get("attendees") == ["andy.chen@example.test"], r)
    r = tm.j(in_session(s8, manage.draft_event_change, "update", event_id="e3", new_title="Call with Andy P."))
    check("event_update drafts store the guests (none here)", s8.drafts[r["draft_id"]].get("attendees") == [], r)
    check("so a guest-less move is tier 1", vp.tier_of(s8.drafts[r["draft_id"]])["tier"] == 1)


def check_autonomy():
    log = fresh_log()
    check("voice is a confirmer", "voice" in autonomy.CONFIRMERS)
    check("Proof shows heard and stt_conf", {"heard", "stt_conf"} <= set(autonomy.PROOF_EXTRA))
    for by in ("voice", "user", "voice"):
        obs.log_decision("commit_pending", workflow="replies", card_kind="email_send", origin="asked", confirmed_by=by,
                         edited=False, verified=True, ok=True, level="suggest", heard="send it" if by == "voice" else None,
                         stt_conf=0.91 if by == "voice" else None)
    check("voice approvals count toward the streak like a click", autonomy.streak("replies") == 3, autonomy.streak("replies"))
    obs.log_decision("commit_pending", workflow="replies", card_kind="email_send", origin="asked", confirmed_by="policy",
                     edited=False, verified=True, ok=True, level="suggest")
    check("Auto's own writes still prove nothing", autonomy.streak("replies") == 3, autonomy.streak("replies"))
    obs.log_decision("commit_pending", workflow="replies", card_kind="email_send", origin="asked", confirmed_by="voice",
                     edited=True, verified=True, ok=True, level="suggest", heard="send it", stt_conf=0.9)
    check("an edited voice approval ends the streak", autonomy.streak("replies") == 0, autonomy.streak("replies"))
    p = [r for r in autonomy.proof(10, "replies") if r.get("confirmed_by") == "voice"]
    check("Proof rows show confirmed_by voice, heard and stt_conf", p and p[0].get("heard") == "send it" and p[0].get("stt_conf") == 0.9, p[:1])


def main() -> int:
    import logging

    logging.getLogger("assistant").setLevel(logging.CRITICAL)
    fresh_log()
    g, c, _ = tm.setup()
    install_fakes()
    voicestate.reset_all()
    for fn, args in ((check_conf_min, ()), (check_table, ()), (check_readback, (g, c)), (check_checks, (g, c)),
                     (check_manage, (g, c)), (check_autonomy, ())):
        try:
            fn(*args)
        except Exception as e:  # a crash in one group is a failure, not the end of the run
            import traceback
            check(f"{fn.__name__} crashed", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1200:]}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} voice_policy checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
