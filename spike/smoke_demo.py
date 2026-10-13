"""Live rehearsal of the founder demo against the real account, through assistant.flow
(the same functions the web page calls).

Safe by construction:
- NOTHING IS SENT. Email cards are either skipped or saved to Gmail Drafts. The
  drafts this script saves are addressed to your own address with the subject
  "[Vaani smoke] ...", so they can never reach anyone else.
- No mailbox changes: the triage card is created and checked, then skipped.
- Autonomy levels and the replies streak are put back the way they were at the end
  (the decision rows stay in logs/tools.jsonl; the snooze marker hides them from
  the next streak).

Steps: brief on open -> "What does X want?" on the first Needs a reply row ->
follow-ups listed and a nudge card skipped -> triage card at One-tap is "inline" ->
3 clean approvals (save as draft) -> promotion offer for replies -> Proof rows.

Run: VAANI_BRIEF_ON_OPEN=1 VAANI_FOLLOWUPS=1 VAANI_AUTONOMY=1 VAANI_PROMOTION=1 VAANI_PROMOTION_N=3 \\
     .venv/bin/python spike/smoke_demo.py      (exit 1 on any failure)
"""

from __future__ import annotations

import copy
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for k, v in {"VAANI_BRIEF_ON_OPEN": "1", "VAANI_FOLLOWUPS": "1", "VAANI_AUTONOMY": "1", "VAANI_PROMOTION": "1",
             "VAANI_PROMOTION_N": "3", "VAANI_AUTO_ALLOWED": "triage"}.items():
    os.environ.setdefault(k, v)

from assistant import agent, autonomy, flow, google_auth, manage, memory, obs  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def step(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"\n       {str(detail)[:400]}"))


def main() -> int:
    own = google_auth.connected_email().lower()
    saved = copy.deepcopy(memory.load().get("autonomy") or {})
    session = agent.new_session()
    try:
        # 1. brief on open
        b = flow.open_chat(session)
        keys = [g.get("key") for g in (b or {}).get("groups") or []]
        step("brief on open has the five groups", b and not b.get("error") and keys[:5] == ["needs_reply", "follow_ups", "worth_knowing", "updates", "promotions"], b and (b.get("error") or keys))
        step("brief stamp fields", b and b.get("built_at") and b.get("covers_days"), b)

        # 2. "What does X want?" on the first person row
        rows = next((g["rows"] for g in b["groups"] if g["key"] == "needs_reply"), []) if b else []
        if rows:
            out = flow.action(session, "reply", message_id=rows[0]["id"])
            step("reply action summarises the email", out.get("reply") and not out.get("pending_draft"), out.get("reply"))
        else:
            step("reply action skipped (no person email in the brief window)", True)

        # 3. follow-ups: nudge card, then skip it
        fus = next((g["rows"] for g in b["groups"] if g["key"] == "follow_ups"), []) if b else []
        if fus:
            out = flow.action(session, "nudge", thread_id=fus[0]["thread_id"])
            card = out.get("pending_draft") or {}
            step("nudge card is a follow_ups email card", card.get("kind") == "email_send" and card.get("workflow") == "follow_ups", card)
            out = flow.skip(session, card.get("draft_id", ""))
            step("nudge skipped, nothing sent", "nothing was sent" in (out.get("reply") or "").lower(), out.get("reply"))
        else:
            step("nudge skipped (no follow-up candidates today)", True)

        # 4. triage at One-tap gives an inline card; skip it (no mailbox change)
        autonomy.set_level("triage", "one_tap")
        out = flow.action(session, "archive_all")
        card = out.get("pending_draft") or {}
        if card:
            step("triage card at One-tap is inline", out.get("decision") == "inline", out.get("decision"))
            flow.skip(session, card.get("draft_id", ""))
        else:
            step("triage card skipped (no promotions in the window)", True, out.get("reply"))

        # 5. three clean approvals (saved as drafts to yourself) -> offer
        autonomy.set_level("replies", "suggest")
        autonomy.snooze_offer("replies")  # start the streak from zero for this run
        offer = None
        for i in range(1, 4):
            # The card is made in code (the streak, not the model's drafting, is under test here);
            # the reply flow in step 2 already exercises the model.
            res = flow._code_turn(session, f"Draft test email {i} to {own}.",
                                  lambda i=i: manage.draft_email(to=[own], subject=f"[Vaani smoke] test draft {i}",
                                                                 body="This is a Vaani smoke-test draft. Please ignore."),
                                  "asked", time.perf_counter())
            out = flow.apply_policy(session, res)
            card = out.get("pending_draft") or {}
            ok = card.get("kind") == "email_send" and own in [a.lower() for a in card.get("to") or []]
            step(f"draft {i}: card to own address", ok, card or out.get("reply"))
            if not ok:
                break
            out = flow.confirm(session, card["draft_id"], "save_draft")
            rec = out.get("receipt") or {}
            step(f"draft {i}: saved to Gmail drafts and verified", rec.get("status") == "SAVED_DRAFT" and rec.get("verified"), rec)
            offer = out.get("offer") or offer
        step("promotion offer after 3 clean approvals", offer and offer.get("workflow") == "replies", offer)

        # 6. proof rows
        p = flow.proof(workflow="replies", limit=5)
        step("proof lists the approvals", len(p.get("rows") or []) >= 3, p)
    except Exception as e:
        step("smoke demo crashed", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")
    finally:
        def restore(data):
            data["autonomy"] = saved or data.get("autonomy")
        try:
            memory.update(restore)
            autonomy.snooze_offer("replies")
        except Exception:
            pass
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} demo smoke steps passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
