"""Offline checks for round 2: Undo on a carried-out triage card, the "two skips in a row"
step-back offer, the optional model rewrite of nudges, and the eval status in /proof.

No model, no Google: the fake Gmail from evals.test_manage, a temp log and temp memory.
Run: .venv/bin/python -m evals.test_undo
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from assistant import agent, autonomy, brief, flow, followups, llm, obs
from evals import test_manage as tm

RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    if not ok:
        print(f"FAIL  {name}\n      {str(detail)[:400]}")


def fresh_log() -> Path:
    path = Path(tempfile.mkdtemp()) / "tools.jsonl"
    obs.TOOLS_LOG = path
    return path


def tools_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def archive_card(session, ids):
    res = flow._code_turn(session, "Archive the promotions in my inbox.",
                          lambda: tm.manage.draft_triage("archive", origin="brief", message_ids=ids), "brief", time.perf_counter())
    return res.pending_draft


def check_undo():
    log = fresh_log()
    g, _c, _ = tm.setup()
    brief.invalidate = lambda user_id=None: None
    s = agent.new_session()
    card = archive_card(s, ["m3"])
    check("triage card made in code", card and card.get("kind") == "email_modify", card)
    out = flow.confirm(s, card["draft_id"])
    rec = out.get("receipt") or {}
    check("archive carried out and verified", "INBOX" not in g.msgs["m3"]["labelIds"] and rec.get("verified"), (g.msgs["m3"]["labelIds"], rec))
    undo = rec.get("undo") or {}
    check("receipt offers Undo for the same card", undo.get("draft_id") == card["draft_id"] and "unarchive" in undo.get("text", ""), undo)

    out = flow.action(s, "undo", draft_id=card["draft_id"])
    rec2 = out.get("receipt") or {}
    check("Undo puts the email back in the inbox, verified", "INBOX" in g.msgs["m3"]["labelIds"] and rec2.get("verified"), (g.msgs["m3"]["labelIds"], rec2))
    check("Undo result is marked as an undo", out.get("decision") == "undo", out.get("decision"))
    check("an undo receipt offers no second Undo", not rec2.get("undo"), rec2)
    check("the original card is marked undone", s.drafts[card["draft_id"]].get("undone") is True)
    again = flow.action(s, "undo", draft_id=card["draft_id"])
    check("undoing twice does nothing", "nothing to undo" in (again.get("reply") or "").lower(), again.get("reply"))

    rows = tools_rows(log)
    tools_seen = [r.get("tool") for r in rows]
    check("undo_applied is logged", "undo_applied" in tools_seen, tools_seen)
    commits = [r for r in rows if r.get("tool") == "commit_pending"]
    check("the undo write is logged as confirmed_by undo", any(r.get("confirmed_by") == "undo" for r in commits), commits)
    check("the triage streak ends after an undo", autonomy.streak("triage") == 0, autonomy.streak("triage"))

    label_card = {"kind": "email_modify", "committed": {"status": "DONE"}, "action": "label", "ids": ["m3"]}
    check("label changes offer no Undo", autonomy.undo_plan(label_card) is None)
    check("an uncarried card offers no Undo", autonomy.undo_plan({"kind": "email_modify", "action": "archive", "ids": ["m3"]}) is None)
    check("trash can be undone (untrash)", (autonomy.undo_plan({"kind": "email_modify", "committed": {"status": "DONE"}, "action": "trash", "ids": ["m3"]}) or {}).get("action") == "untrash")


def check_undo_at_auto():
    log = fresh_log()
    tm.setup()
    os.environ["VAANI_AUTONOMY"] = "1"
    autonomy.set_level("triage", "auto")
    draft = {"draft_id": "dX", "kind": "email_modify", "workflow": "triage", "committed": {"status": "DONE"}, "action": "archive", "ids": ["m3"]}
    autonomy.record_undo(draft)
    check("undoing an Auto action steps the workflow down to One-tap", autonomy.level_of("triage") == "one_tap", autonomy.level_of("triage"))
    rows = tools_rows(log)
    check("the safety step-down is logged with reason undone",
          any(r.get("tool") == "level_change" and r.get("reason") == "undone" and r.get("by") == "safety" for r in rows), rows)
    os.environ["VAANI_AUTONOMY"] = "0"


def skip_row(wf="triage"):
    obs.log_decision("skip_pending", workflow=wf, card_kind="email_modify", origin="brief", level=autonomy.level_of(wf))


def clean_commit(wf="triage"):
    obs.log_decision("commit_pending", workflow=wf, card_kind="email_modify", origin="brief", confirmed_by="inline",
                     edited=False, verified=True, ok=True, level=autonomy.level_of(wf))


def check_stepback():
    fresh_log()
    tm.setup()
    os.environ["VAANI_AUTONOMY"] = "1"
    skip_row(); skip_row()
    check("no step-back at Suggest", autonomy.stepback_for("triage") is None)
    autonomy.set_level("triage", "one_tap")
    skip_row()
    check("one skip at One-tap is not enough", autonomy.stepback_for("triage") is None)
    skip_row()
    offer = autonomy.stepback_for("triage")
    check("two skips in a row at One-tap bring a step-back offer",
          offer and offer["kind"] == "step_back" and offer["from_level"] == "one_tap" and offer["to_level"] == "suggest", offer)
    check("the offer names its own buttons", offer and offer["accept_action"] == "step_back" and offer["snooze_action"] == "keep", offer)
    autonomy.keep_level("triage")
    check("Keep hides the offer until two more skips", autonomy.stepback_for("triage") is None)
    skip_row(); clean_commit(); skip_row()
    check("an approval in between breaks the run", autonomy.stepback_for("triage") is None)
    skip_row()
    check("two fresh skips bring it back", autonomy.stepback_for("triage") is not None)
    autonomy.accept_stepback("triage")
    check("accepting steps back one level", autonomy.level_of("triage") == "suggest", autonomy.level_of("triage"))
    try:
        autonomy.accept_stepback("triage")
        refused = False
    except ValueError:
        refused = True
    check("a step-back that is not due is refused", refused)
    os.environ["VAANI_AUTONOMY"] = "0"
    autonomy.set_level("triage", "one_tap")
    skip_row(); skip_row()
    check("no step-back offers when autonomy is off", autonomy.stepback_for("triage") is None)

    # flow.skip carries the offer to the page
    os.environ["VAANI_AUTONOMY"] = "1"
    fresh_log()
    tm.setup()
    autonomy.set_level("triage", "one_tap")
    brief.invalidate = lambda user_id=None: None
    s = agent.new_session()
    first = archive_card(s, ["m3"])
    out1 = flow.skip(s, first["draft_id"])
    second = archive_card(s, ["m3"])
    out2 = flow.skip(s, second["draft_id"])
    check("first skip: no offer yet", out1.get("offer") is None, out1.get("offer"))
    check("second skip: /skip returns the step-back offer", (out2.get("offer") or {}).get("kind") == "step_back", out2.get("offer"))
    os.environ["VAANI_AUTONOMY"] = "0"


class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


def check_nudge_rewrite():
    row = {"to_name": "Priya Shah", "to_email": "priya@acme.example", "subject": "Deck for Friday", "expects_reply": True}
    template = followups.nudge_body(row, "Alex Kim")
    real = llm.complete
    try:
        llm.complete = lambda messages, tools: _Msg("Hi Priya,\n\nJust checking in on the deck for Friday. Could you send it over when you can?\n\nThanks,\nAlex")
        out = followups.rewrite_nudge(row, template, "Alex Kim")
        check("a good rewrite is used", out.startswith("Hi Priya") and out != template, out)
        llm.complete = lambda messages, tools: _Msg("Hi [Name], following up. Best, [Your Name]")
        check("a rewrite with placeholders falls back to the template", followups.rewrite_nudge(row, template, "Alex Kim") == template)
        llm.complete = lambda messages, tools: _Msg("Following up on the deck. Thanks!")
        check("a rewrite that drops the person or sign-off falls back", followups.rewrite_nudge(row, template, "Alex Kim") == template)

        def boom(messages, tools):
            raise llm.LLMError("down")
        llm.complete = boom
        check("a model outage falls back to the template", followups.rewrite_nudge(row, template, "Alex Kim") == template)
    finally:
        llm.complete = real


def check_eval_status():
    tmp = Path(tempfile.mkdtemp()) / "eval_status.json"
    flow.EVAL_STATUS = tmp
    check("no eval run yet: proof has evals None", flow.eval_status() is None)
    tmp.write_text(json.dumps({"ran_at": "2026-10-13T17:00:00+00:00", "passed": 10, "total": 10, "all_passed": True,
                               "suites": [{"name": "manage", "passed": 10, "total": 10, "ok": True}]}))
    check("proof reads the last eval run", (flow.eval_status() or {}).get("total") == 10)
    from evals import all as eval_all
    check("the runner parses a suite's last line", eval_all.COUNT_RE.findall("x\n106/106 manage checks passed\n") == [("106", "106")])


def check_demo_scope():
    from assistant import context
    os.environ["VAANI_DEMO_LABEL"] = ""
    check("demo scope off: queries unchanged", context.gmail_scope("in:inbox newer_than:3d") == "in:inbox newer_than:3d")
    os.environ["VAANI_DEMO_LABEL"] = "vaani-demo"
    check("demo scope on: every query is fenced to the label",
          context.gmail_scope("in:inbox newer_than:3d") == "(in:inbox newer_than:3d) label:vaani-demo", context.gmail_scope("in:inbox newer_than:3d"))
    check("an OR query is fenced as a whole", context.gmail_scope('from:"Ravi" OR to:"Ravi"') == '(from:"Ravi" OR to:"Ravi") label:vaani-demo')
    check("the label is not added twice", context.gmail_scope("label:vaani-demo is:unread") == "label:vaani-demo is:unread")
    g, _c, _ = tm.setup()
    seen = []
    real_list = g.messages().__class__.list

    def spy(self, userId="me", q="", maxResults=10, **kw):
        seen.append(q)
        return real_list(self, userId=userId, q=q, maxResults=maxResults, **kw)

    g.messages().__class__.list = spy
    try:
        tm.tools.list_recent_emails(5, "in:inbox newer_than:3d")
    finally:
        g.messages().__class__.list = real_list
        os.environ["VAANI_DEMO_LABEL"] = ""
    check("list_recent_emails searches only the demo label", seen and all("label:vaani-demo" in q for q in seen), seen)


def main() -> int:
    for fn in (check_undo, check_undo_at_auto, check_stepback, check_nudge_rewrite, check_eval_status, check_demo_scope):
        try:
            fn()
        except Exception as e:  # a crash in one group is a failure, not the end of the run
            import traceback
            check(f"{fn.__name__} crashed", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} undo, step-back and demo-scope checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
