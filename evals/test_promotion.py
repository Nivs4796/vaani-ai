"""Offline checks for earned promotion: the streak computed from logs/tools.jsonl, the
offer, accept, snooze, ceilings, per-user separation and the Proof rows.

Most checks write synthetic decision rows to a temp tools.jsonl; the last block earns an
offer through the real commit path (fake Gmail from evals/test_manage.py).

Run: .venv/bin/python -m evals.test_promotion   (exit 1 on any failure)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update({"VAANI_AUTONOMY": "1", "VAANI_AUTO_ALLOWED": "triage", "VAANI_PROMOTION": "1", "VAANI_PROMOTION_N": "3"})

from assistant import agent, autonomy, context, google_auth, manage, memory, obs  # noqa: E402
from evals import test_manage as tm  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp())
CLOCK = [datetime.now(timezone.utc) - timedelta(days=1)]


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


def fresh():
    """Empty log and memory: every scenario starts from Suggest with no history."""
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)
    obs.TOOLS_LOG = TMP / "tools.jsonl"
    obs.TURNS_LOG = TMP / "turns.jsonl"
    memory.MEMORY_FILE = TMP / "memory.json"
    memory.USERS_DIR = TMP / "users"
    CLOCK[0] = datetime.now(timezone.utc) - timedelta(days=1)


def ts() -> str:
    CLOCK[0] += timedelta(seconds=1)
    return CLOCK[0].isoformat(timespec="milliseconds")


def commit(wf="replies", user=None, **fields):
    kind = {"replies": "email_send", "follow_ups": "email_send", "triage": "email_modify"}.get(wf, "event_update")
    row = {"workflow": wf, "origin": "asked", "card_kind": kind, "confirmed_by": "user", "edited": False,
           "verified": True, "ok": True, "level": "suggest", "ms": 640}
    row.update(fields)
    obs.log_decision("commit_pending", ts=ts(), user_id=user, **row)


def clean(n=1, **kw):
    for _ in range(n):
        commit(**kw)


def pseudo(tool, wf="replies", user=None, **fields):
    obs.log_decision(tool, ts=ts(), user_id=user, workflow=wf, **fields)


def rows(tool=None):
    try:
        out = [json.loads(x) for x in obs.TOOLS_LOG.read_text().splitlines() if x.strip()]
    except OSError:
        return []
    return [r for r in out if tool is None or r.get("tool") == tool]


def main() -> int:
    # ---- no log file at all
    fresh()
    check("missing log file: streak 0", autonomy.streak("replies") == 0 and not obs.TOOLS_LOG.exists())
    check("missing log file: state and proof still work", all(r["streak"] == 0 for r in autonomy.get_state().values()) and autonomy.proof() == [])
    check("missing log file: no offer", autonomy.offer_for("replies") is None)

    # ---- clean approvals build the streak; the offer appears at N
    clean(2)
    check("two clean approvals: streak 2", autonomy.streak("replies") == 2, autonomy.streak("replies"))
    check("below N: no offer", autonomy.offer_for("replies") is None and not autonomy.get_state()["replies"]["offer_due"])
    clean()
    offer = autonomy.offer_for("replies")
    want = "Vaani has been right 3 times in a row on replies (no edits, all verified). Move replies from Suggest to One-tap?"
    check("offer at N", offer and offer["from_level"] == "suggest" and offer["to_level"] == "one_tap" and offer["streak"] == 3, offer)
    check("offer text", (offer or {}).get("text") == want, (offer or {}).get("text"))
    check("offer shape", offer and set(offer) == {"workflow", "from_level", "to_level", "streak", "text"}, offer)
    check("state says offer_due", autonomy.get_state()["replies"]["offer_due"] and autonomy.get_state()["replies"]["remaining"] == 0)
    autonomy.offer_for("replies")
    check("the offer is logged once", len(rows("promotion_offer")) == 1, rows("promotion_offer"))
    clean()
    check("a promotion_offer row does not break the streak", autonomy.streak("replies") == 4)

    # ---- what resets the streak
    for label, bad in [("edited", lambda: commit(edited=True)), ("skipped", lambda: pseudo("skip_pending", card_kind="email_send")),
                       ("failed", lambda: commit(ok=False, verified=False, error="ERROR: timeout")),
                       ("unverified", lambda: commit(verified=False))]:
        fresh()
        clean(2)
        bad()
        check(f"{label} resets the streak", autonomy.streak("replies") == 0, autonomy.streak("replies"))
        clean()
        check(f"counting restarts after {label}", autonomy.streak("replies") == 1, autonomy.streak("replies"))

    # ---- what is ignored
    fresh()
    clean(2)
    commit(confirmed_by="policy")
    commit(confirmed_by="policy", ok=False, verified=False)
    check("policy rows are ignored", autonomy.streak("replies") == 2, autonomy.streak("replies"))
    commit(confirmed_by="inline")
    check("an inline approve counts", autonomy.streak("replies") == 3)
    clean(3, wf="triage")
    check("other workflows are ignored", autonomy.streak("replies") == 3 and autonomy.streak("triage") == 3)
    with obs.TOOLS_LOG.open("a") as f:
        f.write(json.dumps({"ts": ts(), "user_id": None, "tool": "commit_pending", "workflow": "replies", "ok": False}) + "\n")
        f.write('{"ts": "half a line", "workflow": "replies"\n')
    check("rows without decision fields and broken lines are ignored", autonomy.streak("replies") == 3)
    clean(5, user="user-b")
    commit(user="user-b", edited=True)
    check("other users' rows are ignored", autonomy.streak("replies") == 3, autonomy.streak("replies"))
    check("streak for an explicit user", autonomy.streak("replies", user_id="user-b") == 0)
    clean(2, user="user-b")
    tok = context.current_user.set({"id": "user-b", "email": "b@example.test"})
    try:
        check("streak for the signed-in user", autonomy.streak("replies") == 2, autonomy.streak("replies"))
        check("the signed-in user has no offer yet", autonomy.offer_for("replies") is None)
        autonomy.set_level("triage", "one_tap")
    finally:
        context.current_user.reset(tok)
    check("levels are per user", autonomy.get_state()["triage"]["level"] == "suggest" and autonomy.get_state("user-b")["triage"]["level"] == "one_tap")

    # ---- snooze: only clean approvals after it count
    fresh()
    clean(3)
    check("offer due before snooze", autonomy.offer_for("replies") is not None)
    row = autonomy.snooze_offer("replies")
    snz = rows("promotion_snooze")[-1]
    check("snooze resets the streak", row["streak"] == 0 and not row["offer_due"], row)
    check("snooze is logged and stored", snz.get("streak") == 3 and memory.load()["autonomy"]["replies"]["snoozed_at"] == snz["ts"], snz)
    CLOCK[0] = datetime.now(timezone.utc) - timedelta(hours=1)
    clean(3)  # written after the snooze but stamped before it (a slow worker): still ignored
    check("rows stamped before snoozed_at are ignored", autonomy.streak("replies") == 0, autonomy.streak("replies"))
    CLOCK[0] = datetime.now(timezone.utc) + timedelta(minutes=1)
    clean(3)
    check("three clean approvals after the snooze: offer again", (autonomy.offer_for("replies") or {}).get("streak") == 3)

    # ---- accept raises one level and starts a new streak
    row = autonomy.accept_offer("replies")
    acc = rows("promotion_accept")[-1]
    check("accept raises replies to one_tap", row["level"] == "one_tap" and memory.load()["autonomy"]["replies"]["level"] == "one_tap", row)
    check("accept is logged", acc.get("from_level") == "suggest" and acc.get("to_level") == "one_tap" and acc.get("streak") == 3, acc)
    check("accept starts a new streak", row["streak"] == 0)
    try:
        autonomy.accept_offer("replies")
        check("accept without a due offer is refused", False, "no error")
    except ValueError as e:
        check("accept without a due offer is refused", "no promotion" in str(e), e)
    clean(4)
    check("replies at the one_tap ceiling get no offer", autonomy.offer_for("replies") is None and autonomy.get_state()["replies"]["ceiling"] == "one_tap")
    os.environ["VAANI_AUTO_ALLOWED"] = "triage,replies"
    autonomy.set_level("replies", send_unlocked=True)
    check("unlock alone keeps the streak", autonomy.streak("replies") == 4)
    off = autonomy.offer_for("replies")
    check("unlocked and allowed replies are offered auto", off and off["to_level"] == "auto" and "from One-tap to Auto" in off["text"], off)
    os.environ["VAANI_AUTO_ALLOWED"] = "triage"
    check("not allowed again: no offer", autonomy.offer_for("replies") is None)

    # ---- triage can earn auto; off never gets an offer; the flag turns offers off
    fresh()
    autonomy.set_level("triage", "one_tap")
    clean(3, wf="triage")
    off = autonomy.offer_for("triage")
    check("triage at one_tap is offered auto", off and off["to_level"] == "auto", off)
    check("accept takes triage to auto", autonomy.accept_offer("triage")["level"] == "auto")
    clean(3, wf="triage")
    check("auto is the top: no offer", autonomy.offer_for("triage") is None)
    os.environ["VAANI_AUTO_ALLOWED"] = ""
    autonomy.set_level("triage", "one_tap")
    clean(3, wf="triage")
    check("triage not allowed auto: no offer at one_tap", autonomy.offer_for("triage") is None and autonomy.get_state()["triage"]["ceiling"] == "one_tap")
    os.environ["VAANI_AUTO_ALLOWED"] = "triage"
    autonomy.set_level("scheduling", "off")
    clean(3, wf="scheduling")
    check("a workflow turned off gets no offer", autonomy.offer_for("scheduling") is None)
    clean(3)
    os.environ["VAANI_PROMOTION"] = "0"
    check("VAANI_PROMOTION=0: no offer", autonomy.offer_for("replies") is None and not autonomy.get_state()["replies"]["offer_due"])
    os.environ["VAANI_PROMOTION"] = "1"
    del os.environ["VAANI_PROMOTION_N"]
    check("N defaults to 5", autonomy.get_state()["replies"]["n_needed"] == 5 and autonomy.offer_for("replies") is None)
    os.environ["VAANI_PROMOTION_N"] = "3"
    check("N from VAANI_PROMOTION_N", autonomy.get_state()["replies"]["n_needed"] == 3 and autonomy.offer_for("replies") is not None)

    # ---- proof: this user's decision rows, newest first
    fresh()
    clean(2)
    commit(wf="triage", verified=False)
    pseudo("skip_pending", card_kind="email_send")
    clean(1, user="user-b")
    obs.log_decision("list_recent_emails", ts=ts())  # not a decision row
    p = autonomy.proof()
    check("proof is newest first", [r["tool"] for r in p] == ["skip_pending", "commit_pending", "commit_pending", "commit_pending"], [r["tool"] for r in p])
    check("proof rows have the contract keys", all(set(autonomy.PROOF_KEYS) <= set(r) for r in p) and p[1]["workflow"] == "triage" and p[1]["verified"] is False, p[:2])
    check("proof filters by workflow and limit", len(autonomy.proof(workflow="replies")) == 3 and len(autonomy.proof(limit=1)) == 1)
    check("proof shows only this user", len(autonomy.proof(user_id="user-b")) == 1)

    # ---- end to end: three real confirmed emails earn the offer
    fresh()
    g, _c, _tmr = tm.setup()
    memory.MEMORY_FILE = TMP / "memory.json"
    google_auth.get_services = lambda *a, **k: {"gmail": g, "calendar": _c}
    s = agent.new_session()
    token = obs.current_session.set(s)
    try:
        got = None
        for i, variant in enumerate(["", "save_draft", ""]):
            s.turn += 1
            tm.user(s, f"Email sam.lee@example.org about test {i}")
            did = json.loads(manage.draft_email(["sam.lee@example.org"], f"Test {i}", f"Hi Sam,\nTest {i}.\nAlex"))["draft_id"]
            check(f"no offer before approval {i + 1}", autonomy.offer_for("replies") is None)
            agent.run_turn(s, confirm_draft_id=did, confirm_variant=variant or None)
            got = autonomy.offer_for("replies")
        check("three real clean approvals (one saved as a draft) earn the offer", got and got["streak"] == 3, got)
        s.turn += 1
        tm.user(s, "Email sam.lee@example.org about one more")
        did = json.loads(manage.draft_email(["sam.lee@example.org"], "More", "Hi Sam,\nMore.\nAlex"))["draft_id"]
        autonomy.record_skip(s.drafts[did])
        agent.run_turn(s, cancel_draft_id=did)
        check("skipping a card ends the streak", autonomy.streak("replies") == 0 and autonomy.offer_for("replies") is None)
    finally:
        obs.current_session.reset(token)

    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:400]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} promotion checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
