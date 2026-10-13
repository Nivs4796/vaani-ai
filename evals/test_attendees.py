"""Offline check of the attendee rule and contact saving: no model, no Google calls.

Uses a temporary memory file, so data/memory.json is never touched.
Run: .venv/bin/python -m evals.test_attendees   (exit 1 on any failure)
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from assistant import agent, memory, obs, tools

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def user(session, text: str) -> None:
    session.messages.append({"role": "user", "content": f"{agent._now_tag()} {text}"})


def start_tomorrow(hour: int) -> str:
    return (datetime.now() + timedelta(days=1)).replace(hour=hour, minute=0).strftime("%Y-%m-%dT%H:%M")


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "memory.json"
    tmp.write_text(json.dumps(memory.DEFAULT))
    memory.MEMORY_FILE = tmp
    s = agent.new_session()
    token = obs.current_session.set(s)
    try:
        # 1. typed address -> allowed on a draft
        user(s, 'make call event tomorrow at 4 PM with "sam.lee@example.org"')
        r = json.loads(tools.draft_calendar_event("Call with Sam Lee", start_tomorrow(16), 30, ["sam.lee@example.org"]))
        check("typed address is allowed as attendee", r.get("attendees") == ["sam.lee@example.org"], str(r)[:160])

        # 2. address never typed (as if read from an email) -> refused, with a completable next step
        r = tools.draft_calendar_event("Call with Mallory", start_tomorrow(17), 30, ["mallory@evil.example"])
        check("untyped address is refused", r.startswith("ERROR:") and "has not typed it" in r, r[:160])
        check("refusal asks the user to type the address", "type the exact email address" in r, r[:200])
        check("refusal no longer suggests editing memory.json", "memory.json" not in r, r[:200])

        # 3. same refusal again in the same chat -> say-it-once wording
        r2 = tools.draft_calendar_event("Call with Mallory", start_tomorrow(17), 30, ["mallory@evil.example"])
        check("repeat refusal says stop retrying", "already refused once" in r2 and "do not retry" in r2, r2[:200])

        # 4. save_contact: untyped refused, typed saved, then usable by name in a NEW chat
        r = tools.save_contact("Mallory", "mallory@evil.example")
        check("save_contact refuses an untyped address", r.startswith("ERROR:"), r[:160])
        user(s, "sam.lee@example.net")
        user(s, "yes add that")
        r = json.loads(tools.save_contact("Sam Lee", "sam.lee@example.net"))
        check("save_contact saves a typed address", r.get("status") == "ADDED", str(r))
        saved = memory.load()["contacts"]
        check("contact written to memory", {"name": "Sam Lee", "email": "sam.lee@example.net"} in saved, str(saved))

        s2 = agent.new_session()
        obs.current_session.reset(token)
        token = obs.current_session.set(s2)
        user(s2, "Add a call with Sam Lee tomorrow at 11am")
        r = json.loads(tools.find_contact("Sam Lee"))
        check("find_contact returns the saved contact by name", r.get("source") == "memory" and r.get("count") == 1, str(r)[:200])
        r = json.loads(tools.draft_calendar_event("Call with Sam Lee", start_tomorrow(11), 30, ["sam.lee@example.net"]))
        check("saved contact is allowed in a new chat without retyping", r.get("attendees") == ["sam.lee@example.net"], str(r)[:160])

        # 5. remember("X's email is Y") becomes a contact, not a free-text fact
        user(s2, "Priya's email is priya@acme.example")
        r = json.loads(tools.remember("Priya's email is priya@acme.example"))
        data = memory.load()
        check("remember routes an email fact to save_contact", r.get("status") == "ADDED"
              and {"name": "Priya", "email": "priya@acme.example"} in data["contacts"]
              and not any("priya@" in f for f in data["facts"]), str(r))

        # 5b. deterministic "which Andy?" only for scheduling requests with an ambiguous first name
        q = tools.ambiguous_contact_question("Add a call with Andy tomorrow at 3pm")
        check("which-contact question for an ambiguous scheduling request",
              q == "You have 2 contacts named Andy: Andy Chen and Andy Patel. Which one should I invite?", str(q))
        check("no question when the full name is given", tools.ambiguous_contact_question("Add a call with Andy Chen tomorrow at 3pm") is None)
        check("no question without scheduling intent", tools.ambiguous_contact_question("What did Andy say?") is None)
        check("no question for a single-match name", tools.ambiguous_contact_question("Book a call with Sam tomorrow") is None)

        # 5c. "tomorrow" in the request is checked against the drafted date, also when answering a question
        s3 = agent.new_session()
        obs.current_session.reset(token)
        token = obs.current_session.set(s3)
        user(s3, "Add a call with Andy tomorrow at 3pm")
        s3.messages.append({"role": "assistant", "content": "You have 2 contacts named Andy: Andy Chen and Andy Patel. Which one should I invite?"})
        user(s3, "Andy Chen")
        wrong = (datetime.now() + timedelta(days=2)).replace(hour=15, minute=0).strftime("%Y-%m-%dT%H:%M")
        r = json.loads(tools.draft_calendar_event("Call with Andy Chen", wrong, 30, ["andy.chen@example.test"]))
        right = (datetime.now(agent._tz()) + timedelta(days=1)).date().isoformat()
        check("day-after-tomorrow is corrected to tomorrow, same time", str(r.get("start", "")).startswith(right + "T15:00")
              and "corrected" in str(r.get("date_note", "")), str(r)[:200])
        r = json.loads(tools.draft_calendar_event("Call with Andy Chen", start_tomorrow(15), 30, ["andy.chen@example.test"]))
        check("tomorrow is accepted", r.get("status") == "DRAFT", str(r)[:160])

        # 6. false "confirmed / will be created" claims are removed while a draft is pending
        bad = "Priya has been saved as a contact. The event is confirmed and will be created shortly.\n\n- Title: Call with Priya"
        fixed = agent._scrub_pending_claims(bad, pending=True)
        check("pending-claim scrub removes the false sentence", "is confirmed" not in fixed and "saved as a contact" in fixed
              and "- Title: Call with Priya" in fixed and fixed.endswith(agent.PENDING_STATUS_LINE), fixed)
        ok_text = "Here's the draft for your call with Priya. Would you like to confirm and create this event?"
        check("pending-claim scrub keeps a normal draft reply", agent._scrub_pending_claims(ok_text, pending=True) == ok_text)
        check("pending-claim scrub is off with no pending draft", agent._scrub_pending_claims(bad, pending=False) == bad)

        # 6. nothing above wrote to Google: drafts only
        check("no draft was created on Google", not any(d.get("event_id") for d in list(s.drafts.values()) + list(s2.drafts.values()) + list(s3.drafts.values())))
    finally:
        obs.current_session.reset(token)

    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {detail}"))
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} attendee checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
