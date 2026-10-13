"""Live check of the Gmail and Calendar write paths against the real account.

Safe by construction, and it cleans up after itself:
- Email: NOTHING IS SENT. Drafts are saved to Gmail Drafts (the "Save to Gmail drafts"
  button path), verified, then moved to Trash (recoverable for 30 days).
- Mailbox: one promotional email from your inbox is marked read/unread, archived/unarchived
  and starred/unstarred, and its original labels are restored and checked at the end.
- Senders are picked from your own inbox (the newest email from a person, the newest
  promotion), so nothing about your mail is hardcoded here.
- Calendar: a throwaway "PA test event" at 07:00 tomorrow is created, moved and
  deleted through the same Confirm path the web page uses.
- Model routing: natural-language requests are checked for the right card; email and
  mailbox cards from the model are cancelled, never confirmed.

Run: .venv/bin/python spike/smoke_manage.py   (exit 1 on any failure)
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import agent, google_auth, manage, memory, obs, tools  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
OWN = google_auth.connected_email().lower()


def step(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"\n       {str(detail)[:400]}"))


def user(session, text):
    session.messages.append({"role": "user", "content": f"{agent._now_tag(text, session)} {text}"})


def in_session(session, fn, *a, **kw):
    token = obs.current_session.set(session)
    try:
        return fn(*a, **kw)
    finally:
        obs.current_session.reset(token)


def labels_of(gmail, mid):
    return set(gmail.users().messages().get(userId="me", id=mid, format="minimal").execute(num_retries=2).get("labelIds") or [])


def main() -> int:
    svc = google_auth.get_services()
    gmail, cal = svc["gmail"], svc["calendar"]
    tz = tools._tz()
    tmr = (datetime.now(tz) + timedelta(days=1)).date()
    trash_after: list[str] = []      # Gmail draft message ids to move to Trash
    delete_after: list[str] = []     # calendar event ids to delete
    promo_id, promo_before = None, None
    # Pick real targets from this inbox: the newest email from a person, and the newest promotion.
    listing = json.loads(in_session(agent.new_session(), tools.list_recent_emails, 25, "in:inbox newer_than:14d") or "{}")
    rows = listing.get("messages", []) if isinstance(listing, dict) else []
    person = next((r for r in rows if r.get("group") == "needs_reply"), None)
    promo = next((r for r in rows if r.get("group") == "promotions"), None)
    who = (agent._recipient_first_name(person["from"]) or person["sender"].split()[0]) if person else None
    promo_sender = re.sub(r"\s*\(.*?\)", "", promo["sender"]).strip() if promo else None
    me = str(memory.load().get("name") or "Me")
    print(f"    targets: person={who!r}, promotion sender={promo_sender!r}")
    try:
        # ------------------------------------------------------------ 1. email drafts (never sent)
        s = agent.new_session()
        user(s, f"Email {OWN} a test note from the assistant")
        r = json.loads(in_session(s, manage.draft_email, [OWN], "PA test draft (safe to delete)", "This is a test draft from the assistant. It was never sent."))
        res = agent.run_turn(s, confirm_draft_id=r["draft_id"], confirm_variant="save_draft")
        d = s.drafts[r["draft_id"]].get("committed") or {}
        if d.get("message_id"):
            trash_after.append(d["message_id"])
        step("new email saved to Gmail drafts, verified, nothing sent", d.get("status") == "SAVED_DRAFT" and d.get("verified") and not s.drafts[r["draft_id"]].get("sent_id"), res.reply)

        if person:
            s2 = agent.new_session()
            user(s2, f"Reply to {who} saying thanks, I will review it")
            r = json.loads(in_session(s2, manage.draft_email, None, "", f"Hi {who},\nThanks, I will review it.\n{me}", reply_to_message_id=person["id"]))
            dd = s2.drafts[r["draft_id"]]
            res = agent.run_turn(s2, confirm_draft_id=r["draft_id"], confirm_variant="save_draft")
            c = dd.get("committed") or {}
            if c.get("message_id"):
                trash_after.append(c["message_id"])
            thread_ok = False
            if c.get("message_id"):
                got = gmail.users().messages().get(userId="me", id=c["message_id"], format="minimal").execute()
                thread_ok = got.get("threadId") == gmail.users().messages().get(userId="me", id=person["id"], format="minimal").execute().get("threadId")
            step(f"reply draft lands in {who}'s thread (saved, not sent)", c.get("status") == "SAVED_DRAFT" and thread_ok, {"to": dd.get("to"), "subject": dd.get("subject"), "thread_ok": thread_ok})
        else:
            step("reply draft (skipped: no email from a person in the last 14 days)", True)

        # ------------------------------------------------------------ 2. mailbox changes on one promotion, restored after
        if promo:
            promo_id = promo["id"]
            promo_before = labels_of(gmail, promo_id) & {"INBOX", "UNREAD", "STARRED"}
            s3 = agent.new_session()
            for act, check_fn in [("mark_read", lambda L: "UNREAD" not in L), ("mark_unread", lambda L: "UNREAD" in L),
                                  ("archive", lambda L: "INBOX" not in L), ("unarchive", lambda L: "INBOX" in L),
                                  ("star", lambda L: "STARRED" in L), ("unstar", lambda L: "STARRED" not in L)]:
                r = json.loads(in_session(s3, manage.modify_emails, act, message_ids=[promo_id]))
                before = labels_of(gmail, promo_id)
                res = agent.run_turn(s3, confirm_draft_id=r["draft_id"])
                after = labels_of(gmail, promo_id)
                step(f"{act} on one promotion, verified", check_fn(after) and "1 of 1" in res.reply, {"before": sorted(before), "after": sorted(after), "reply": res.reply})
        else:
            step("mailbox changes (skipped: no promotional email in the last 14 days)", True)

        # ------------------------------------------------------------ 3. calendar: create, move, delete a throwaway event
        s4 = agent.new_session()
        user(s4, "Add a PA test event tomorrow at 7am")
        r = json.loads(in_session(s4, tools.draft_calendar_event, "PA test event", f"{tmr.isoformat()}T07:00", 30))
        agent.run_turn(s4, confirm_draft_id=r["draft_id"])
        eid = s4.drafts[r["draft_id"]].get("event_id")
        if eid:
            delete_after.append(eid)
        step("throwaway event created through Confirm", bool(eid), r)

        free = json.loads(in_session(s4, manage.find_free_time, tmr.isoformat(), 30, 1, "06:00", "09:00"))
        slots = [(x["from"], x["to"]) for x in free.get("free", [])]
        step("free time sees the new event (06:00-09:00 window)", ("06:00", "07:00") in slots and ("07:30", "09:00") in slots, slots)

        user(s4, "Move the PA test event tomorrow to 8am")
        r = json.loads(in_session(s4, manage.draft_event_change, "update", event_query="PA test event", on_date="tomorrow", new_start_iso=f"{tmr.isoformat()}T08:00"))
        res = agent.run_turn(s4, confirm_draft_id=r["draft_id"])
        got = cal.events().get(calendarId="primary", eventId=eid).execute() if eid else {}
        step("event moved to 08:00 through Confirm, verified", got.get("start", {}).get("dateTime", "")[11:16] == "08:00" and res.reply.startswith("Updated and verified"), res.reply)

        # ------------------------------------------------------------ 4. model routing (cards only; email/mailbox cards cancelled)
        s5 = agent.new_session()
        probes = [
            (f"Email {OWN} saying the test passed", "email_send"),
            *([(f"Reply to {who} saying thanks, I will review it and get back by Friday", "email_send")] if who else []),
            *([(f"Forward the email from {promo_sender} to {OWN} saying FYI", "email_send")] if promo_sender else []),
            ("Archive all my promotional emails", "email_modify"),
            ("Move the PA test event tomorrow to 8:30am", "event_update"),
        ]
        for text, kind in probes:
            rr = agent.run_turn(s5, text)
            pend = rr.pending_draft or {}
            step(f"model prepares a {kind} card for: {text!r}", pend.get("kind") == kind and not pend.get("committed"),
                 {"tools": [(t["name"], t["ok"]) for t in rr.tool_calls], "reply": rr.reply[:200]})
            if kind == "email_send" and text.startswith("Reply to"):
                step(f"the model's reply card answers {who}'s email in its thread", pend.get("mode") == "reply" and bool(pend.get("thread_id"))
                     and pend.get("subject", "").startswith("Re: "), {k: pend.get(k) for k in ("mode", "to", "subject", "thread_id")})
            if kind == "email_send" and "Forward" in text:
                step("the forward card has Fwd: subject, the original text and the typed recipient", pend.get("mode") == "forward"
                     and pend.get("subject", "").startswith("Fwd: ") and pend.get("to") == [OWN] and "Forwarded message" in pend.get("body", ""),
                     {k: pend.get(k) for k in ("mode", "to", "subject")})
            if pend:
                agent.run_turn(s5, cancel_draft_id=pend["draft_id"])  # never confirm model-made email/mailbox cards here

        # ------------------------------------------------------------ 4b. conversation follow-ups (cards cancelled, nothing sent)
        sc = agent.new_session()
        r1 = agent.run_turn(sc, "Can you tell me what's in my calendar events tomorrow?")
        r2 = agent.run_turn(sc, "yes")
        asked = "?" in r1.reply
        step("'yes' after the assistant's own question is answered in context", (not asked) or "no typed confirmations" not in r2.footer,
             {"asked": asked, "reply": r2.reply[:160], "footer": r2.footer})
        if who:
            sf = agent.new_session()
            agent.run_turn(sf, "can you check my latest emails ?")
            agent.run_turn(sf, f"what does {who.lower()} want ?")
            rr = agent.run_turn(sf, "reply to them saying I will call them on Monday to go through it")
            pend = rr.pending_draft or {}
            body = pend.get("body") or ""
            step(f"'reply to them saying ...' drafts a reply to {who}'s email, addressed to them", pend.get("mode") == "reply"
                 and body.startswith(f"Hi {who}") and " you " in f" {body} " and "call them" not in body, {"mode": pend.get("mode"), "body": body[:200]})
            if pend:
                agent.run_turn(sf, cancel_draft_id=pend["draft_id"])
            rf = agent.run_turn(sf, f"forward it to {OWN}")
            pend = rf.pending_draft or {}
            step("'forward it to <typed address>' forwards the same email", pend.get("mode") == "forward" and pend.get("to") == [OWN],
                 {k: pend.get(k) for k in ("mode", "to", "subject")})
            if pend:
                agent.run_turn(sf, cancel_draft_id=pend["draft_id"])
        else:
            step("follow-up flows (skipped: no email from a person in the last 14 days)", True)

        # ------------------------------------------------------------ 5. delete the throwaway event through Confirm
        s6 = agent.new_session()
        user(s6, "Delete the PA test event tomorrow")
        r = json.loads(in_session(s6, manage.draft_event_change, "delete", event_query="PA test event", on_date="tomorrow"))
        res = agent.run_turn(s6, confirm_draft_id=r.get("draft_id", ""))
        gone = False
        if eid:
            try:
                gone = cal.events().get(calendarId="primary", eventId=eid).execute().get("status") == "cancelled"
            except Exception as e:
                gone = tools._http_status(e) in (404, 410)
        if gone:
            delete_after.remove(eid)
        step("throwaway event deleted through Confirm, verified", gone, res.reply)

        # ------------------------------------------------------------ 6. day brief (read-only)
        rb = agent.run_turn(agent.new_session(), "Brief my day")
        step("day brief built in code", "**Today," in rb.reply and "Free today" in rb.reply and "Needs a reply" in rb.reply, rb.reply[:200])
    except Exception:
        traceback.print_exc()
        step("smoke script crashed", False, traceback.format_exc().splitlines()[-1])
    finally:
        for mid in trash_after:
            try:
                gmail.users().messages().trash(userId="me", id=mid).execute(num_retries=2)
                print(f"    cleanup: test draft {mid} moved to Trash")
            except Exception as e:
                print(f"    cleanup FAILED for draft {mid}: {e}")
        for eid in delete_after:
            try:
                cal.events().delete(calendarId="primary", eventId=eid, sendUpdates="none").execute(num_retries=2)
                print(f"    cleanup: test event {eid} deleted")
            except Exception as e:
                print(f"    cleanup FAILED for event {eid}: {e}")
        if promo_id is not None and promo_before is not None:
            now_labels = labels_of(gmail, promo_id) & {"INBOX", "UNREAD", "STARRED"}
            add, rem = sorted(promo_before - now_labels), sorted(now_labels - promo_before)
            if add or rem:
                gmail.users().messages().batchModify(userId="me", body={"ids": [promo_id], "addLabelIds": add, "removeLabelIds": rem}).execute(num_retries=2)
            restored = (labels_of(gmail, promo_id) & {"INBOX", "UNREAD", "STARRED"}) == promo_before
            step("promotion's original labels restored", restored, {"before": sorted(promo_before)})
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
