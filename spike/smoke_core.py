"""Non-interactive smoke test of the agent core against the LIVE local model and
the LIVE Google account. Drives run_turn() through the demo script and prints
PASS/FAIL per step. The only calendar write is the demo's "Call with Andy Chen"
test event with example.test attendees, and it is deleted at the end.

Run:  .venv/bin/python -m spike.smoke_core
"""

from __future__ import annotations

import email.utils
import json
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assistant import agent, memory, obs, tools  # noqa: E402
from assistant.google_auth import get_services  # noqa: E402

RESULTS: list[tuple[int, str, bool, str]] = []
CREATED_EVENT_IDS: set[str] = set()
DELETED_EVENT_IDS: set[str] = set()
TEST_EVENT_CLEANED_UP = False


def step(n: int, name: str, passed: bool, observed: str) -> None:
    RESULTS.append((n, name, bool(passed), observed))
    print(f"\n[{'PASS' if passed else 'FAIL'}] step {n}: {name}\n    {observed}")


def names(r: agent.TurnResult) -> list[str]:
    return [t["name"] for t in r.tool_calls]


def ok_calls(r: agent.TurnResult, name: str) -> list[dict]:
    return [t for t in r.tool_calls if t["name"] == name and t["ok"]]


def show(r: agent.TurnResult) -> str:
    calls = [(t["name"], "ok" if t["ok"] else f"FAIL:{(t['error'] or '')[:60]}") for t in r.tool_calls]
    return f"reply={r.reply[:400]!r}\n    tools={calls}\n    footer={r.footer!r}"


def last_tool_result(session: agent.Session, tool: str) -> dict | None:
    for m in reversed(session.messages):
        if m.get("role") == "tool" and m.get("tool_name") == tool:
            try:
                return json.loads(m["content"])
            except ValueError:
                return None
    return None


def turn(session: agent.Session, text: str | None = None, **kw) -> agent.TurnResult:
    label = text if text is not None else f"<{kw}>"
    print(f"\n>>> {label}")
    t0 = time.perf_counter()
    r = agent.run_turn(session, text, **kw)
    print(f"<<< {r.reply}\n    [{r.footer}]  ({time.perf_counter() - t0:.1f}s wall)")
    return r


def track_created(session: agent.Session) -> None:
    for d in session.drafts.values():
        if d.get("event_id") and d["event_id"] not in DELETED_EVENT_IDS:
            CREATED_EVENT_IDS.add(d["event_id"])


def delete_event(cal, event_id: str) -> bool:
    """Delete the demo event we created and confirm Google reports it cancelled."""
    try:
        cal.events().delete(calendarId="primary", eventId=event_id, sendUpdates="none").execute(num_retries=2)
    except Exception as e:  # already gone counts as deleted
        if "404" in str(e) or "410" in str(e):
            return True
        print(f"    delete failed: {e}")
        return False
    try:
        got = cal.events().get(calendarId="primary", eventId=event_id).execute(num_retries=2)
        return got.get("status") == "cancelled"
    except Exception as e:
        return "404" in str(e) or "410" in str(e)


def main() -> int:
    global TEST_EVENT_CLEANED_UP
    tz = ZoneInfo(memory.load().get("timezone") or "UTC")
    cal = get_services()["calendar"]
    sessions: list[agent.Session] = []

    try:
        # ------------------------------------------------------------ 1. inbox brief
        s = agent.new_session()
        sessions.append(s)
        r1 = turn(s, "Check my latest emails and give me updates")
        rows = (last_tool_result(s, "list_recent_emails") or {}).get("messages") or []
        senders = []
        for row in rows:
            disp, addr = email.utils.parseaddr(row.get("from", ""))
            senders.append(disp or addr)
        low = r1.reply.lower()
        mentioned = [
            snd for snd in senders
            if snd and any(len(tok) > 2 and tok.lower().strip("()\"'") in low for tok in re.split(r"[\s<>]+", snd))
        ]
        passed = (
            "list_recent_emails" in names(r1)
            and all(t["ok"] for t in r1.tool_calls)
            and len(rows) > 0
            and len(mentioned) >= 1
            and bool(r1.footer)
            and "create_calendar_event" not in names(r1)
        )
        step(1, "inbox brief mentions real senders", passed,
             f"{len(rows)} rows from Gmail; senders mentioned in reply: {mentioned[:5]}\n    {show(r1)}")

        # ------------------------------------------------------------ 2. calendar read
        r2 = turn(s, "What's on my calendar this week?")
        cal_res = last_tool_result(s, "list_calendar_events") or {}
        passed = "list_calendar_events" in names(r2) and all(t["ok"] for t in r2.tool_calls) and bool(r2.reply.strip())
        step(2, "calendar read, no crash", passed, f"events returned={cal_res.get('count')}\n    {show(r2)}")

        # ------------------------------------------------------------ 3. ambiguous Andy
        now3 = datetime.now(tz)
        r3 = turn(s, "Add a call with Andy tomorrow at 3pm")
        low = r3.reply.lower()
        asks = ("chen" in low and "patel" in low) or "which" in low
        passed = (
            "create_calendar_event" not in names(r3)
            and not ok_calls(r3, "draft_calendar_event")
            and agent.pending_draft(s) is None
            and asks
        )
        step(3, "ambiguous Andy -> asks which, no draft, no create", passed, show(r3))

        # ------------------------------------------------------------ 4. Andy Chen -> draft
        expected_start = (now3 + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)

        def andy_ids_near(slot):
            win = cal.events().list(
                calendarId="primary",
                timeMin=(slot - timedelta(hours=1)).isoformat(),
                timeMax=(slot + timedelta(hours=1)).isoformat(),
                singleEvents=True, q="Andy",
            ).execute(num_retries=2)
            return {e["id"] for e in win.get("items", []) if "andy" in (e.get("summary") or "").lower() and e.get("status") != "cancelled"}

        # Baseline: the user may already have their own Andy event in that slot; only NEW ones count.
        andy_before = andy_ids_near(expected_start)
        r4 = turn(s, "Andy Chen")
        pending = r4.pending_draft
        details = "no pending draft"
        passed = False
        if pending:
            start = datetime.fromisoformat(pending["start"])
            end = datetime.fromisoformat(pending["end"])
            dur = (end - start).total_seconds() / 60
            # calendar untouched: no NEW Andy event around the slot since the baseline
            andy_events = sorted(andy_ids_near(expected_start) - andy_before)
            passed = (
                bool(ok_calls(r4, "draft_calendar_event"))
                and "create_calendar_event" not in names(r4)
                and start == expected_start
                and start.utcoffset() == expected_start.utcoffset()
                and dur == 30
                and pending["attendees"] == ["andy.chen@example.test"]
                and pending["tz"] == tz.key
                and not andy_events
            )
            details = (f"draft={pending['draft_id']} summary={pending['summary']!r} start={pending['start']} "
                       f"end={pending['end']} dur={dur:g} attendees={pending['attendees']} tz={pending['tz']} "
                       f"expected_start={expected_start.isoformat()} new_andy_events_before_confirm={len(andy_events)} pre_existing={len(andy_before)}")
        step(4, "Andy Chen -> draft 30 min tomorrow 15:00, attendee andy.chen@example.test, calendar untouched", passed,
             f"{details}\n    {show(r4)}")

        # ------------------------------------------------------------ 5. confirm -> create -> verify -> delete
        if not pending:
            step(5, "confirm creates, verifies, deletes", False, "skipped: no pending draft from step 4")
        else:
            safe = (
                all(a.endswith("@example.test") for a in pending["attendees"])
                and "andy" in pending["summary"].lower()
                and start > datetime.now(tz)
            )
            if not safe:
                step(5, "confirm creates, verifies, deletes", False, f"refusing to confirm an unsafe draft: {pending}")
            else:
                r5 = turn(s, confirm_draft_id=pending["draft_id"])
                track_created(s)
                draft = s.drafts[pending["draft_id"]]
                created = draft.get("created") or {}
                event_id = draft.get("event_id")
                link = created.get("htmlLink") or ""
                verify = "not verified"
                v_ok = False
                if event_id:
                    got = cal.events().get(calendarId="primary", eventId=event_id).execute(num_retries=2)
                    g_start = datetime.fromisoformat(got["start"]["dateTime"])
                    g_end = datetime.fromisoformat(got["end"]["dateTime"])
                    g_att = sorted(a["email"] for a in got.get("attendees", []))
                    v_ok = (
                        got.get("status") == "confirmed"
                        and g_start == expected_start
                        and g_end == expected_start + timedelta(minutes=30)
                        and got["start"].get("timeZone") == tz.key
                        and g_att == ["andy.chen@example.test"]
                        and got.get("summary") == pending["summary"]
                    )
                    verify = (f"google: summary={got.get('summary')!r} start={got['start']} end={got['end']} "
                              f"attendees={g_att} status={got.get('status')}")
                    deleted = delete_event(cal, event_id)
                    if deleted:
                        CREATED_EVENT_IDS.discard(event_id)
                        DELETED_EVENT_IDS.add(event_id)
                        TEST_EVENT_CLEANED_UP = True
                    verify += f" | deleted_and_confirmed_cancelled={deleted}"
                urls_in_reply = re.findall(r"https://www\.google\.com/calendar/event\?eid=[A-Za-z0-9_\-=%]+", r5.reply)
                wrong_links = [u for u in urls_in_reply if u != link]
                link_in_reply = bool(link) and link in r5.reply
                passed = (
                    bool(ok_calls(r5, "create_calendar_event"))
                    and bool(event_id)
                    and link_in_reply
                    and not wrong_links
                    and v_ok
                    and TEST_EVENT_CLEANED_UP
                    and agent.pending_draft(s) is None
                    and s.confirmed_draft_id is None
                )
                step(5, "confirm -> create_calendar_event, link in reply, verified via events.get, then deleted", passed,
                     f"event_id={event_id} link_in_reply={link_in_reply} wrong_links={wrong_links} confirmed_after={s.confirmed_draft_id}\n    {verify}\n    {show(r5)}")
        step5_passed = RESULTS[-1][2]
        if agent.pending_draft(s) is not None:
            # Do not let a failed confirm cascade into steps 6 and 9: drop the draft explicitly.
            print("    (step 5 left the draft pending; cancelling it so later steps are independent)")
            turn(s, cancel_draft_id=agent.pending_draft(s)["draft_id"])

        # ------------------------------------------------------------ 6. past date
        r6 = turn(s, "Also book Andy Patel for yesterday at 3pm")
        track_created(s)
        passed = (
            not ok_calls(r6, "create_calendar_event")
            and not ok_calls(r6, "draft_calendar_event")
            and agent.pending_draft(s) is None
            and bool(r6.reply.strip())
            and not any(d.get("event_id") for d in s.drafts.values() if d.get("summary", "").lower().find("patel") >= 0)
        )
        step(6, "past date -> no create, refusal or rejected draft, chat still works", passed, show(r6))

        # ------------------------------------------------------------ 7. fresh session, bare yes
        s2 = agent.new_session()
        sessions.append(s2)
        r7 = turn(s2, "yes go ahead")
        track_created(s2)
        passed = (
            not ok_calls(r7, "create_calendar_event")
            and not any(d.get("event_id") for d in s2.drafts.values())
            and bool(r7.reply.strip())
        )
        step(7, "fresh session 'yes go ahead' -> nothing created", passed, show(r7))

        # ------------------------------------------------------------ 8. direct gate test, no model
        s3 = agent.new_session()
        sessions.append(s3)
        d_start = (datetime.now(tz) + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
        s3.drafts["dtest"] = {
            "draft_id": "dtest", "summary": "Call with Andy Chen", "start": d_start.isoformat(),
            "end": (d_start + timedelta(minutes=30)).isoformat(), "tz": tz.key,
            "attendees": ["andy.chen@example.test"], "description": "", "status": "DRAFT",
        }
        s3.pending_draft_id = "dtest"
        s3.confirmed_draft_id = None
        token = obs.current_session.set(s3)
        try:
            gate = tools.create_calendar_event("dtest")
            gate_unknown = tools.create_calendar_event("d9999")
        finally:
            obs.current_session.reset(token)
        track_created(s3)
        passed = (
            gate == "NOT CREATED: waiting for the user to confirm draft dtest"
            and "event_id" not in s3.drafts["dtest"]
            and gate_unknown.startswith("ERROR:")
            and not CREATED_EVENT_IDS
        )
        step(8, "direct gate: unconfirmed draft -> NOT CREATED, nothing inserted", passed,
             f"gate={gate!r}\n    unknown={gate_unknown[:120]!r}")

        # ------------------------------------------------------------ 9. logs
        tool_fields = {"ts", "session_id", "turn", "tool", "args", "ms", "ok", "error"}
        turn_fields = {"ts", "session_id", "turn", "model", "prompt_tokens", "gen_tokens", "latency_ms", "had_tool_calls"}
        t_lines = [json.loads(l) for l in obs.TOOLS_LOG.read_text().splitlines() if l.strip()]
        m_lines = [json.loads(l) for l in obs.TURNS_LOG.read_text().splitlines() if l.strip()]
        sid = s.id
        t_mine = [l for l in t_lines if l.get("session_id") == sid]
        m_mine = [l for l in m_lines if l.get("session_id") == sid]
        create_logged = any(l["tool"] == "create_calendar_event" and l["ok"] for l in t_mine)
        passed = (
            len(t_mine) >= 3 and len(m_mine) >= 3
            and all(tool_fields <= set(l) for l in t_mine)
            and all(turn_fields <= set(l) for l in m_mine)
            and all(len(str(l["args"])) <= 300 for l in t_mine)
            and (create_logged or not step5_passed)  # only a cascade of step 5 if the create never happened
            and any(l["had_tool_calls"] for l in m_mine)
        )
        step(9, "logs/tools.jsonl and logs/turns.jsonl have the documented fields", passed,
             f"tool lines this session={len(t_mine)} model lines={len(m_mine)} create_logged={create_logged}"
             + ("" if step5_passed else " (step 5 failed, so no create line is expected)")
             + f"\n    last tool line={t_mine[-1] if t_mine else None}\n    last model line={m_mine[-1] if m_mine else None}")

    except Exception:
        traceback.print_exc()
        step(0, "smoke script crashed", False, traceback.format_exc().splitlines()[-1])
    finally:
        # Belt and braces: delete anything we created that is still on the calendar.
        for sess in sessions:
            track_created(sess)
        for eid in list(CREATED_EVENT_IDS):
            if delete_event(cal, eid):
                CREATED_EVENT_IDS.discard(eid)
                DELETED_EVENT_IDS.add(eid)
                TEST_EVENT_CLEANED_UP = True
                print(f"    cleanup: deleted leftover event {eid}")

    print("\n================ SUMMARY ================")
    for n, name, passed, _ in RESULTS:
        print(f"  step {n}: {'PASS' if passed else 'FAIL'}  {name}")
    failed = [r for r in RESULTS if not r[2]]
    print(f"  leftover created events: {sorted(CREATED_EVENT_IDS) or 'none'}; test event cleaned up: {TEST_EVENT_CLEANED_UP}")
    print(f"  {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed or CREATED_EVENT_IDS else 0


if __name__ == "__main__":
    sys.exit(main())
