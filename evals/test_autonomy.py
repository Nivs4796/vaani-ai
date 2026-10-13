"""Offline checks for the autonomy ladder: decide(), set_level(), workflow_of(), draft
tagging, the decision rows the commit paths write, safety demotion, draft_nudge and
draft_triage.

No model, no network: the fake Gmail and Calendar from evals/test_manage.py, a temp
memory directory and a temp logs/tools.jsonl.

Run: .venv/bin/python -m evals.test_autonomy   (exit 1 on any failure)
"""

from __future__ import annotations

import base64
import copy
import email
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.update({"VAANI_AUTONOMY": "1", "VAANI_AUTO_ALLOWED": "triage", "VAANI_PROMOTION": "1", "VAANI_PROMOTION_N": "3"})

from assistant import agent, autonomy, context, google_auth, manage, memory, obs, tools  # noqa: E402
from evals import test_manage as tm  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


def j(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


def log_rows(tool: str | None = None) -> list[dict]:
    try:
        rows = [json.loads(x) for x in obs.TOOLS_LOG.read_text().splitlines() if x.strip()]
    except OSError:
        return []
    return [r for r in rows if tool is None or r.get("tool") == tool]


class FakeCalInsert(tm.FakeCal):
    def insert(self, calendarId="primary", body=None, sendUpdates=None):
        def run():
            ev = copy.deepcopy(body)
            ev.update({"status": "confirmed", "htmlLink": f"https://cal/{body['id']}"})
            self.ev[body["id"]] = ev
            return copy.deepcopy(ev)
        return tm.Req(run)


def setup():
    g, c0, tmr = tm.setup()  # fake Gmail + Calendar, temp memory.json
    c = FakeCalInsert()
    c.ev = c0.ev
    google_auth.get_services = lambda *a, **k: {"gmail": g, "calendar": c}
    tmp = Path(tempfile.mkdtemp())
    obs.TOOLS_LOG = tmp / "tools.jsonl"
    obs.TURNS_LOG = tmp / "turns.jsonl"
    memory.USERS_DIR = tmp / "users"
    # The user's own sent message, waiting on Priya (To) and Ravi (Cc).
    g.msgs["s1"] = {
        "id": "s1", "threadId": "thread-s1", "labelIds": ["SENT"], "snippet": "Any update on the quote?",
        "payload": {"mimeType": "text/plain", "body": {"data": tm._b64("Any update on the quote?")}, "headers": [
            {"name": "From", "value": f"Alex Kim <{tm.OWN}>"},
            {"name": "To", "value": f"Priya Shah <priya@acme.example>, {tm.OWN}"},
            {"name": "Cc", "value": "Ravi Mehta <ravi@studio.example>"},
            {"name": "Subject", "value": "Quote for the design package"},
            {"name": "Date", "value": "Thu, 08 Oct 2026 10:00:00 +0530"},
            {"name": "Message-ID", "value": "<s1@mail.example>"}]},
    }
    g.msgs["m2"]["labelIds"].append("IMPORTANT")
    return g, c, tmr


def draft(kind="email_modify", origin="brief", **kw) -> dict:
    d = {"kind": kind, "origin": origin, **kw}
    d["workflow"] = autonomy.workflow_of(d)
    return d


def prefs(level, send_unlocked=False, wf=None) -> dict:
    return {"autonomy": {w: {"level": level, "send_unlocked": send_unlocked, "snoozed_at": None}
                         for w in ([wf] if wf else autonomy.WORKFLOWS)}}


def check_decide():
    # workflow_of
    for d, want in [({"kind": "email_send"}, "replies"), ({"kind": "email_send", "origin": "followup"}, "follow_ups"),
                    ({"kind": "email_send", "origin": "brief"}, "replies"), ({"kind": "email_modify"}, "triage"),
                    ({"kind": "event_update"}, "scheduling"), ({"kind": "event_delete"}, "scheduling"),
                    ({"kind": "event_rsvp"}, "scheduling"), ({"summary": "Call"}, "scheduling")]:
        check(f"workflow_of {d}", autonomy.workflow_of(d) == want, autonomy.workflow_of(d))

    archive = draft(action="archive")
    trash = draft(action="trash")
    untrash = draft(action="untrash")
    reply = draft(kind="email_send")
    nudge = draft(kind="email_send", origin="followup")
    event = draft(kind="event_update")
    every = [archive, trash, untrash, reply, nudge, event] + [draft(action=a) for a in sorted(autonomy.REVERSIBLE)]
    for level in autonomy.LEVELS:
        for unlocked in (False, True):
            for d in every:
                asked = dict(d, origin="asked")
                got = autonomy.decide(asked, prefs(level, unlocked))
                check(f"origin asked waits ({d['workflow']}/{d.get('action', d['kind'])}, {level}, unlocked={unlocked})", got == "wait", got)
    for d in every:
        for level in ("off", "suggest"):
            got = autonomy.decide(d, prefs(level, True))
            check(f"{level} waits ({d['workflow']}/{d.get('action', d['kind'])})", got == "wait", got)
        got = autonomy.decide(d, prefs("one_tap", True))
        check(f"one_tap is inline ({d['workflow']}/{d.get('action', d['kind'])})", got == "inline", got)
    for a in sorted(autonomy.REVERSIBLE):
        got = autonomy.decide(draft(action=a), prefs("auto"))
        check(f"auto triage {a} is auto", got == "auto", got)
    check("auto triage trash is never auto", autonomy.decide(trash, prefs("auto", True)) == "inline")
    check("auto triage untrash is never auto", autonomy.decide(untrash, prefs("auto", True)) == "inline")
    check("auto replies without unlock is inline", autonomy.decide(reply, prefs("auto")) == "inline")
    check("auto replies unlocked but not allowed is inline", autonomy.decide(reply, prefs("auto", True)) == "inline")
    check("auto scheduling unlocked but not allowed is inline", autonomy.decide(event, prefs("auto", True)) == "inline")
    os.environ["VAANI_AUTO_ALLOWED"] = "triage,replies,follow_ups"
    check("auto replies unlocked and allowed is auto", autonomy.decide(reply, prefs("auto", True)) == "auto")
    check("auto follow-ups unlocked and allowed is auto", autonomy.decide(nudge, prefs("auto", True)) == "auto")
    check("allowed but still locked is inline", autonomy.decide(reply, prefs("auto", False)) == "inline")
    os.environ["VAANI_AUTO_ALLOWED"] = ""
    check("triage not allowed: auto level is inline", autonomy.decide(archive, prefs("auto")) == "inline")
    os.environ["VAANI_AUTO_ALLOWED"] = "triage"
    check("prefs as the autonomy dict", autonomy.decide(archive, prefs("auto")["autonomy"]) == "auto")
    check("prefs as one workflow's dict", autonomy.decide(archive, {"level": "one_tap"}) == "inline")
    check("missing origin counts as asked", autonomy.decide({"kind": "email_modify", "action": "archive"}, prefs("auto")) == "wait")
    os.environ["VAANI_AUTONOMY"] = "0"
    check("VAANI_AUTONOMY=0 always waits", all(autonomy.decide(d, prefs("auto", True)) == "wait" for d in every))
    os.environ["VAANI_AUTONOMY"] = "1"
    check("prefs from memory (default suggest) waits", autonomy.decide(archive) == "wait")


def check_set_level():
    try:
        autonomy.set_level("replies", "auto")
        check("set_level refuses auto for a workflow not allowed", False, "no error")
    except ValueError as e:
        check("set_level refuses auto for a workflow not allowed", "not allowed" in str(e), e)
    os.environ["VAANI_AUTO_ALLOWED"] = "triage,replies"
    try:
        autonomy.set_level("replies", "auto")
        check("set_level refuses auto while sending is locked", False, "no error")
    except ValueError as e:
        check("set_level refuses auto while sending is locked", "locked" in str(e), e)
    check("a refused set_level changed nothing", autonomy.get_state()["replies"]["level"] == "suggest")
    row = autonomy.set_level("replies", "auto", send_unlocked=True)
    check("set_level auto with unlock", row["level"] == "auto" and row["send_unlocked"] and row["ceiling"] == "auto", row)
    row = autonomy.set_level("replies", send_unlocked=False)
    check("locking sending again drops auto to one_tap", row["level"] == "one_tap" and not row["send_unlocked"], row)
    os.environ["VAANI_AUTO_ALLOWED"] = "triage"
    row = autonomy.set_level("triage", "auto")
    check("triage can be auto when allowed", row["level"] == "auto" and row["ceiling"] == "auto", row)
    os.environ["VAANI_AUTO_ALLOWED"] = ""
    try:
        autonomy.set_level("triage", "auto")
        check("triage auto refused when VAANI_AUTO_ALLOWED is empty", False, "no error")
    except ValueError as e:
        check("triage auto refused when VAANI_AUTO_ALLOWED is empty", "not allowed" in str(e), e)
    os.environ["VAANI_AUTO_ALLOWED"] = "triage"
    for bad in [("nope", "suggest"), ("replies", "turbo")]:
        try:
            autonomy.set_level(*bad)
            check(f"set_level rejects {bad}", False, "no error")
        except ValueError:
            check(f"set_level rejects {bad}", True)
    row = autonomy.set_level("triage", "one-tap")
    check("set_level accepts one-tap spelling", row["level"] == "one_tap", row)
    changes = [r for r in log_rows("level_change") if r.get("by") == "user"]
    check("level changes are logged", len(changes) >= 3 and all(r.get("from_level") and r.get("to_level") for r in changes), changes[-1:] if changes else None)
    saved = memory.load()["autonomy"]
    check("levels persist in memory", saved["triage"]["level"] == "one_tap" and saved["replies"]["level"] == "one_tap", saved)
    state = autonomy.get_state()
    keys = {"key", "title", "level", "send_unlocked", "streak", "n_needed", "ceiling", "offer_due", "auto_allowed"}
    check("get_state has every workflow with the contract keys", set(state) == set(autonomy.WORKFLOWS) and all(keys <= set(r) for r in state.values()), state)
    check("auto_allowed reflects the flag", state["triage"]["auto_allowed"] and not state["replies"]["auto_allowed"], state)
    for wf in autonomy.WORKFLOWS:
        autonomy.set_level(wf, "suggest", send_unlocked=False)


def check_obs():
    tok = context.current_user.set({"id": "user-a", "email": "a@example.test"})
    try:
        row = obs.log_decision("skip_pending", workflow="replies")
    finally:
        context.current_user.reset(tok)
    base = {"ts", "user_id", "session_id", "turn", "tool", "ms", "ok", "error"}
    check("log_decision writes the base fields", base <= set(row) and row["user_id"] == "user-a" and row["ms"] == 0 and row["ok"] is True, row)
    check("log_decision row is in tools.jsonl", log_rows("skip_pending")[-1] == json.loads(json.dumps(row)), log_rows("skip_pending")[-1:])
    check("origin and confirmer defaults", obs.current_origin.get() == "asked" and obs.current_confirmer.get() == "user")


def main() -> int:
    g, c, tmr = setup()
    check_decide()
    check_set_level()
    check_obs()

    s = agent.new_session()
    token = obs.current_session.set(s)
    try:
        # ---- tool rows carry user_id; list_recent_emails gains "important"
        out = j(tools.list_recent_emails(10, "in:inbox"))
        imp = {m["id"]: m.get("important") for m in out.get("messages", [])}
        check("list_recent_emails rows say important", imp.get("m2") is True and imp.get("m1") is False, imp)
        last = log_rows("list_recent_emails")[-1]
        check("every tool row has user_id", "user_id" in last and last["user_id"] is None, last)

        # ---- draft tagging: a new card, a same-turn re-draft, and a revision in a later turn
        s.turn += 1
        tm.user(s, "Email sam.lee@example.org that I will join the 4pm call")
        d1 = s.drafts[j(manage.draft_email(["sam.lee@example.org"], "Joining at 4", "Hi Sam,\nI'll join at 4.\nAlex"))["draft_id"]]
        check("email card is tagged replies / asked", d1.get("workflow") == "replies" and d1.get("origin") == "asked" and d1.get("proposed_hash"), {k: d1.get(k) for k in autonomy.DRAFT_TAGS})
        check("a first card is not edited", d1.get("edited") is False)
        d1b = s.drafts[j(manage.draft_email(["sam.lee@example.org"], "Joining at 4", "Hi Sam,\nI will join at 4.\nAlex"))["draft_id"]]
        check("the model re-drafting in the same turn is not an edit", d1b.get("edited") is False and d1["status"] == "SUPERSEDED", d1b.get("edited"))
        s.turn += 1
        tm.user(s, "yes, but make it 5pm")
        d2 = s.drafts[j(manage.draft_email(["sam.lee@example.org"], "Joining at 5", "Hi Sam,\nI'll join at 5.\nAlex"))["draft_id"]]
        check("a revision in a later turn is edited", d2.get("edited") is True and d2.get("revision_of") == d1b["draft_id"], {k: d2.get(k) for k in ("edited", "revision_of")})
        check("proposed_hash is stable", autonomy.proposed_hash(d2) == d2["proposed_hash"])
        probe = dict(d1b, body="changed after drafting")
        check("a body changed after drafting counts as edited", autonomy.was_edited(probe))
        agent.run_turn(s, confirm_draft_id=d2["draft_id"])
        row = log_rows("commit_pending")[-1]
        want = {"workflow": "replies", "origin": "asked", "card_kind": "email_send", "confirmed_by": "user", "edited": True,
                "verified": True, "ok": True, "level": "suggest", "draft_id": d2["draft_id"]}
        check("commit_pending row carries the decision fields", all(row.get(k) == v for k, v in want.items()), row)
        check("the draft records who confirmed it", d2.get("confirmed_by") == "user")

        # ---- calendar: the model's result is unchanged, the draft is tagged, the create row is a decision row
        s.turn += 1
        tm.user(s, "Add a call with Andy Chen tomorrow at 3pm")
        res = j(tools.draft_calendar_event("Call with Andy Chen", f"{tmr}T15:00", 30, ["andy.chen@example.test"]))
        ev = s.drafts.get(res.get("draft_id"), {})
        check("draft_calendar_event result has no autonomy tags", not set(autonomy.DRAFT_TAGS) & set(res), sorted(res))
        check("calendar draft is tagged scheduling / asked", ev.get("workflow") == "scheduling" and ev.get("origin") == "asked" and ev.get("proposed_hash"), ev)
        r = agent.run_turn(s, confirm_draft_id=ev["draft_id"])
        row = log_rows("commit_pending")[-1]  # Confirm on a new event runs commit_pending, the one write path
        check("the event was created", ev.get("event_id") and r.reply.startswith("Created and verified"), r.reply)
        want = {"workflow": "scheduling", "card_kind": "event_create", "confirmed_by": "user", "edited": False, "verified": True, "ok": True,
                "level": "suggest", "draft_id": ev["draft_id"]}
        check("the event's commit_pending row carries the decision fields", all(row.get(k) == v for k, v in want.items()), row)
        refused = tools.create_calendar_event(ev["draft_id"])
        ev_rows = [x for x in log_rows() if x.get("card_kind") == "event_create" and x.get("draft_id") == ev["draft_id"]]
        check("a repeat create is idempotent and no new decision row", "already created" in refused and len(ev_rows) == 1
              and "card_kind" not in log_rows("create_calendar_event")[-1], refused[:80])

        # ---- draft_nudge: the user's own sent message, its To and Cc, same thread
        s.turn += 1
        n = j(manage.draft_nudge("s1", "Hi Priya,\nJust checking in on the quote.\n[Your Name]"))
        nd = s.drafts.get(n.get("draft_id"), {})
        check("draft_nudge prepares a send card", n.get("status") == "READY_TO_CONFIRM" and nd.get("kind") == "email_send", n)
        check("nudge goes to the original To", nd.get("to") == ["priya@acme.example"], nd.get("to"))
        check("nudge keeps the original Cc, never the user", nd.get("cc") == ["ravi@studio.example"], nd.get("cc"))
        check("nudge is in the same thread", nd.get("thread_id") == "thread-s1" and nd.get("in_reply_to") == "<s1@mail.example>", (nd.get("thread_id"), nd.get("in_reply_to")))
        check("nudge subject is Re:", nd.get("subject") == "Re: Quote for the design package", nd.get("subject"))
        check("nudge is tagged follow_ups / followup", nd.get("workflow") == "follow_ups" and nd.get("origin") == "followup", (nd.get("workflow"), nd.get("origin")))
        check("[Your Name] is filled in the nudge", nd.get("body", "").endswith("Alex"), nd.get("body"))
        check("nothing is sent at nudge time", len(g.sent_raw) == 1, len(g.sent_raw))
        bad = manage.draft_email(["priya@acme.example"], "Hi", "Hello")
        check("the follow-up case does not widen draft_email", bad.startswith("ERROR:") and "recipient" in bad, bad[:120])
        bad = manage.draft_nudge("m1", "Hi")
        check("a nudge on someone else's email is refused", bad.startswith("ERROR:") and "not sent by the user" in bad, bad[:120])
        check("the refused nudge left the card alone", s.pending_draft_id == nd["draft_id"])
        sent_before = len(g.sent_raw)
        agent.run_turn(s, confirm_draft_id=nd["draft_id"])
        sent = g.sent_raw[-1] if len(g.sent_raw) > sent_before else {}
        raw = email.message_from_bytes(base64.urlsafe_b64decode(sent.get("raw", ""))) if sent else None
        check("the confirmed nudge is sent once in the thread", raw is not None and sent.get("threadId") == "thread-s1" and raw["In-Reply-To"] == "<s1@mail.example>", sent.get("threadId"))
        check("sent nudge headers", raw is not None and raw["To"] == "priya@acme.example" and raw["Cc"] == "ravi@studio.example", dict(raw.items()) if raw else None)
        row = log_rows("commit_pending")[-1]
        check("nudge commit row is follow_ups / followup", row.get("workflow") == "follow_ups" and row.get("origin") == "followup" and row.get("verified") is True, row)
        check("model tools are unchanged", not {"draft_nudge", "draft_triage"} & ({t["function"]["name"] for t in tools.TOOL_SCHEMAS} | set(tools.REGISTRY)))

        # ---- draft_triage: an email_modify card with its origin; typed words do not change the verb
        s.turn += 1
        tm.user(s, "mark everything as unread")
        t = j(manage.draft_triage("archive", group="promotions", origin="brief"))
        td = s.drafts.get(t.get("draft_id"), {})
        check("draft_triage creates an email_modify card", td.get("kind") == "email_modify" and td.get("action") == "archive" and sorted(td.get("ids") or []) == ["m2", "m3"], {k: td.get(k) for k in ("kind", "action", "ids")})
        check("draft_triage card has origin brief / triage", td.get("origin") == "brief" and td.get("workflow") == "triage", (td.get("origin"), td.get("workflow")))
        check("origin is reset after draft_triage", obs.current_origin.get() == "asked")
        check("draft_triage needs a group or query", manage.draft_triage("archive").startswith("ERROR:"))
        check("a triage card at suggest waits", autonomy.decide(td) == "wait")
        autonomy.set_level("triage", "one_tap")
        check("a triage card at one_tap is inline", autonomy.decide(td) == "inline")

        # ---- auto: a policy commit works and stays at auto; a failed or unverified one demotes
        autonomy.set_level("triage", "auto")
        check("the brief archive card is auto at auto", autonomy.decide(td) == "auto")
        ctok = obs.current_confirmer.set("policy")
        try:
            agent.run_turn(s, confirm_draft_id=td["draft_id"])
        finally:
            obs.current_confirmer.reset(ctok)
        row = log_rows("commit_pending")[-1]
        check("policy commit row", row.get("confirmed_by") == "policy" and row.get("ok") and row.get("verified") is True and row.get("level") == "auto", row)
        check("a clean auto write stays at auto", autonomy.get_state()["triage"]["level"] == "auto")
        check("archived for real", "INBOX" not in g.msgs["m3"]["labelIds"])

        s.turn += 1
        tr = s.drafts[j(manage.draft_triage("trash", query="from:google"))["draft_id"]]
        check("trash from the brief is never auto", autonomy.decide(tr) == "inline")

        real_modify = tm._Messages.batchModify

        def boom(self, userId="me", body=None):
            def run():
                raise RuntimeError("backend error")
            return tm.Req(run)

        tm._Messages.batchModify = boom
        try:
            s.turn += 1
            fd = s.drafts[j(manage.draft_triage("mark_read", query="from:cloudflare"))["draft_id"]]
            ctok = obs.current_confirmer.set("policy")
            try:
                agent.run_turn(s, confirm_draft_id=fd["draft_id"])
            finally:
                obs.current_confirmer.reset(ctok)
        finally:
            tm._Messages.batchModify = real_modify
        row = log_rows("commit_pending")[-1]
        check("failed write row", row.get("ok") is False and row.get("verified") is False and row.get("workflow") == "triage", row)
        check("a failed auto write demotes triage to one_tap", autonomy.get_state()["triage"]["level"] == "one_tap")
        lc = log_rows("level_change")[-1]
        check("demotion is logged", lc.get("by") == "safety" and lc.get("reason") == "failed" and lc.get("from_level") == "auto" and lc.get("to_level") == "one_tap", lc)
        check("after demotion the card is inline", autonomy.decide(fd) == "inline")

        autonomy.set_level("triage", "auto")
        tm._Messages.batchModify = lambda self, userId="me", body=None: tm.Req(lambda: {})  # accepted, but nothing changes
        try:
            s.turn += 1
            ud = s.drafts[j(manage.draft_triage("mark_read", query="from:cloudflare"))["draft_id"]]
            ctok = obs.current_confirmer.set("policy")
            try:
                agent.run_turn(s, confirm_draft_id=ud["draft_id"])
            finally:
                obs.current_confirmer.reset(ctok)
        finally:
            tm._Messages.batchModify = real_modify
        row = log_rows("commit_pending")[-1]
        check("unverified write row", row.get("ok") is True and row.get("verified") is False, row)
        check("an unverified auto write demotes too", autonomy.get_state()["triage"]["level"] == "one_tap")
        check("unverified demotion reason", log_rows("level_change")[-1].get("reason") == "unverified", log_rows("level_change")[-1:])
        check("a failure below auto changes nothing", autonomy.on_commit_result(ud, False, False) is None and autonomy.get_state()["triage"]["level"] == "one_tap")

        # ---- record_skip
        s.turn += 1
        sk = s.drafts[j(manage.draft_triage("archive", query="from:google"))["draft_id"]]
        r1 = autonomy.record_skip(sk)
        r2 = autonomy.record_skip(sk)
        check("record_skip logs skip_pending once", r1 and r1["tool"] == "skip_pending" and r1["workflow"] == "triage" and r2 is None, r1)
        check("record_skip ignores a carried-out card", autonomy.record_skip(td) is None)
    finally:
        obs.current_session.reset(token)

    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:400]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} autonomy checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
