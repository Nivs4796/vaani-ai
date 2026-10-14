"""Offline checks for the left-rail routes: GET /meetings/today and GET /drafts/recent.

No network, no real calendar: google_auth.get_services() is replaced by a fake calendar,
data/memory.json by a temp file and logs/tools.jsonl by a temp fixture. The routes run
through the real app (TestClient, single mode) so the gate and _call_as are exercised.

Run: .venv/bin/python -m evals.test_rail   (exit 1 on any failure)
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from assistant import context, google_auth, memory, obs, routes_rail, web

CHECKS: list[tuple[str, bool, str]] = []
TZ = ZoneInfo("Asia/Kolkata")


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


# --------------------------------------------------------------------------- fakes


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeCal:
    """events().list() returns the configured items as given (Google's order is not trusted)."""

    def __init__(self, items=None):
        self.items, self.calls, self.fail = list(items or []), [], None

    def events(self):
        return self

    def list(self, **kwargs):
        self.calls.append(kwargs)

        def run():
            if self.fail is not None:
                raise self.fail
            return {"items": copy.deepcopy(self.items)}
        return Req(run)


def setup_memory():
    tmp = Path(tempfile.mkdtemp()) / "memory.json"
    data = copy.deepcopy(memory.DEFAULT)
    data.update({"name": "Alex", "timezone": "Asia/Kolkata",
                 "contacts": [{"name": "Sam Lee", "email": "sam.lee@example.org"}, {"name": "Paula Sanders", "email": "paula@example.org"}]})
    tmp.write_text(json.dumps(data))
    memory.MEMORY_FILE = tmp


def use_calendar(cal):
    google_auth.get_services = lambda *a, **k: {"calendar": cal, "gmail": None}


# --------------------------------------------------------------------------- meetings


def meetings_checks(client: TestClient):
    today = datetime.now(TZ).date()

    def at(h, m=0):
        return datetime.combine(today, time(h, m), TZ).isoformat()

    noon_utc = datetime.combine(today, time(12, 0), TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [
        {"id": "e3", "status": "confirmed", "summary": "Design review with Paula Sanders", "start": {"dateTime": at(15)},
         "end": {"dateTime": at(15, 30)}, "hangoutLink": "https://meet.google.com/abc-defg-hij", "htmlLink": "https://www.google.com/calendar/event?eid=e3"},
        {"id": "e1", "status": "confirmed", "summary": "Call with Ravi Mehta", "start": {"dateTime": at(10)}, "end": {"dateTime": at(10, 30)},
         "location": "Room 4", "htmlLink": "https://www.google.com/calendar/event?eid=e1"},
        {"id": "e0", "status": "cancelled", "summary": "Cancelled sync", "start": {"dateTime": at(9)}, "end": {"dateTime": at(9, 30)}},
        {"id": "e2", "status": "confirmed", "summary": "Lunch with Sam Lee", "start": {"dateTime": noon_utc}, "end": {"dateTime": at(13)},
         "htmlLink": "javascript:alert(1)"},
        {"id": "e4", "status": "confirmed", "start": {"date": today.isoformat()}, "end": {"date": (today + timedelta(days=1)).isoformat()},
         "htmlLink": "https://www.google.com/calendar/event?eid=e4"},
    ]
    cal = FakeCal(items)
    use_calendar(cal)
    r = client.get("/meetings/today")
    j = r.json() if r.status_code == 200 else {}
    check("meetings: 200 with date, count, events", r.status_code == 200 and {"date", "count", "events"} <= set(j), (r.status_code, r.text[:300]))
    check("meetings: date is today in the user's time zone", j.get("date") == today.isoformat(), j.get("date"))
    call = cal.calls[-1] if cal.calls else {}
    lo, hi = datetime.combine(today, time.min, TZ), datetime.combine(today + timedelta(days=1), time.min, TZ)
    check("meetings: asks primary calendar for start to end of today",
          call.get("calendarId") == "primary" and datetime.fromisoformat(call.get("timeMin", "1970-01-01")) == lo
          and datetime.fromisoformat(call.get("timeMax", "1970-01-01")) == hi and call.get("singleEvents") is True, call)
    events = j.get("events") or []
    check("meetings: cancelled events are left out, count matches", j.get("count") == 4 == len(events) and "e0" not in [e.get("id") for e in events],
          [e.get("id") for e in events])
    check("meetings: all-day first, then by start time", [e.get("id") for e in events] == ["e4", "e1", "e2", "e3"], [e.get("id") for e in events])
    check("meetings: every event has exactly the contract fields",
          all(set(e) == {"id", "title", "start", "end", "all_day", "where", "link"} for e in events), events[:1])
    by = {e.get("id"): e for e in events}
    e4, e1, e2, e3 = by.get("e4", {}), by.get("e1", {}), by.get("e2", {}), by.get("e3", {})
    check("meetings: all-day event is a date, ends the same day (inclusive)",
          e4.get("all_day") is True and e4.get("start") == today.isoformat() and e4.get("end") == today.isoformat(), e4)
    check("meetings: untitled event says (no title)", e4.get("title") == "(no title)", e4.get("title"))
    check("meetings: timed event is local ISO with offset", e1.get("start") == f"{today.isoformat()}T10:00+05:30"
          and e1.get("end") == f"{today.isoformat()}T10:30+05:30" and e1.get("all_day") is False, e1)
    check("meetings: UTC start is shown in the user's time zone", e2.get("start") == f"{today.isoformat()}T12:00+05:30", e2.get("start"))
    check("meetings: where is the location, else the meeting service", e1.get("where") == "Room 4" and e3.get("where") == "Google Meet"
          and e2.get("where") == "", (e1.get("where"), e3.get("where"), e2.get("where")))
    check("meetings: https event link kept, anything else dropped",
          e1.get("link") == "https://www.google.com/calendar/event?eid=e1" and e2.get("link") is None, (e1.get("link"), e2.get("link")))
    check("meetings: no error on success", "error" not in j, j.get("error"))

    cal.items = []
    j = client.get("/meetings/today").json()
    check("meetings: empty calendar -> count 0, no error", j.get("count") == 0 and j.get("events") == [] and "error" not in j, j)

    cal.fail = RuntimeError("backend down")
    r = client.get("/meetings/today")
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("meetings: calendar error -> 200 with empty list and an error line",
          r.status_code == 200 and j.get("count") == 0 and j.get("events") == [] and "RuntimeError" in str(j.get("error")), (r.status_code, r.text[:300]))

    cal.fail = google_auth.ReauthRequired("token revoked")
    j = client.get("/meetings/today").json()
    check("meetings: revoked Google token -> asks to sign in again", "sign in again" in str(j.get("error")) and j.get("count") == 0, j)

    def broken(*a, **k):
        raise OSError("no token.json")
    google_auth.get_services = broken
    r = client.get("/meetings/today")
    check("meetings: Google not connected -> still 200", r.status_code == 200 and r.json().get("count") == 0 and r.json().get("error"), r.text[:300])


# --------------------------------------------------------------------------- drafts


def commit_row(ts, *, user=None, ok=True, variant="save_draft", session="s1", draft="d1", kind="email_send", old=False, **extra):
    args = json.dumps({"draft_id": draft, "variant": variant})
    row = {"ts": ts.isoformat(timespec="milliseconds"), "user_id": user, "session_id": session, "turn": 3, "tool": "commit_pending",
           "args": args, "ms": 900, "ok": ok, "error": None if ok else "ERROR: could not carry out the action"}
    if not old:  # rows written before the decision fields existed carry only args
        row.update({"workflow": "replies", "origin": "asked", "card_kind": kind, "confirmed_by": "user", "edited": False,
                    "level": "suggest", "draft_id": draft, "verified": ok})
        if variant:
            row["variant"] = variant
    row.update(extra)
    return json.dumps(row)


def drafts_checks(client: TestClient):
    now = datetime.now(timezone.utc)
    log_file = Path(tempfile.mkdtemp()) / "tools.jsonl"
    obs.TOOLS_LOG = log_file
    m = lambda minutes: now - timedelta(minutes=minutes)  # noqa: E731
    lines = [
        commit_row(m(60), session="s1", draft="d1"),
        commit_row(m(30), session="s1", draft="d1"),  # the same card again ("already carried out")
        commit_row(m(20), ok=False, session="s5", draft="d5"),  # failed save
        commit_row(m(15), variant="", session="s6", draft="d6"),  # a send, not a saved draft
        commit_row(m(25 * 60), session="s7", draft="d7"),  # older than 24 h
        commit_row(m(5), user="u-other", session="s8", draft="d8"),  # someone else's
        commit_row(m(120), old=True, session="s2", draft="d2"),  # old row shape: variant only in args
        commit_row(m(12), kind="email_modify", session="s9", draft="d9"),  # not an email draft
        json.dumps({"ts": m(3).isoformat(), "user_id": None, "tool": "draft_email", "args": "{}", "ok": True}),
        "not json at all",
        commit_row(m(10), session="s3", draft="d3"),
        commit_row(m(8), session="s4", draft="d4"),  # its session belongs to another user
    ]
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    web.SESSIONS["s3"] = web._Entry(session=SimpleNamespace(drafts={"d3": {
        "kind": "email_send", "subject": "Q4 deck", "to": ["sam.lee@example.org", "paula@example.org"],
        "committed": {"status": "SAVED_DRAFT", "message_id": "18c0ffee12345678"}}}), owner=None)
    web.SESSIONS["s4"] = web._Entry(session=SimpleNamespace(drafts={"d4": {"subject": "Not yours", "to": ["x@example.org"]}}), owner="u-other")
    web.SESSIONS["s8"] = web._Entry(session=SimpleNamespace(drafts={"d8": {"subject": "Private", "to": ["x@example.org"]}}), owner="u-other")
    web.SESSIONS["s2"] = web._Entry(session=SimpleNamespace(drafts={"d2": {"subject": "Invoice", "to": ["new.person@example.org"],
                                                                        "committed": {"message_id": "<bad id>"}}}), owner=None)

    r = client.get("/drafts/recent")
    j = r.json() if r.status_code == 200 else {}
    drafts = j.get("drafts") or []
    titles = [d.get("title") for d in drafts]
    check("drafts: 200 with count and drafts", r.status_code == 200 and {"count", "drafts"} <= set(j), (r.status_code, r.text[:300]))
    check("drafts: only successful saved email drafts of this user in 24 h", j.get("count") == 4 == len(drafts), drafts)
    check("drafts: newest first, one per card", titles == ["Email draft", "Q4 deck", "Email draft", "Invoice"], titles)
    check("drafts: every row has title, to, when, link", all(set(d) == {"title", "to", "when", "link"} for d in drafts), drafts[:1])
    d3 = drafts[1] if len(drafts) > 1 else {}
    check("drafts: subject and saved-contact name from the session", d3.get("title") == "Q4 deck" and d3.get("to") == "Sam Lee +1", d3)
    check("drafts: link opens that draft in Gmail", d3.get("link") == "https://mail.google.com/mail/#drafts?compose=18c0ffee12345678", d3.get("link"))
    check("drafts: another user's session is never read", drafts and drafts[0].get("title") == "Email draft" and drafts[0].get("to") == "", drafts[:1])
    check("drafts: duplicate card keeps its newest time", len(drafts) > 2 and abs(datetime.fromisoformat(drafts[2]["when"]) - m(30)) < timedelta(seconds=2),
          drafts[2:3])
    check("drafts: unsafe message id -> plain Gmail Drafts link, address shown when no contact",
          len(drafts) > 3 and drafts[3].get("link") == "https://mail.google.com/mail/#drafts" and drafts[3].get("to") == "new.person@example.org", drafts[3:4])

    token = context.current_user.set({"id": "u-other", "email": "other@example.org"})
    try:
        other = routes_rail.recent_drafts()
    finally:
        context.current_user.reset(token)
    check("drafts: signed-in user sees only their own rows", other.get("count") == 1 and other["drafts"][0]["title"] == "Private", other)

    log_file.write_text("\n".join(commit_row(m(i + 1), session=f"t{i}", draft="d1") for i in range(12)) + "\n", encoding="utf-8")
    j = client.get("/drafts/recent").json()
    check("drafts: at most 10", j.get("count") == 10 == len(j.get("drafts") or []), j.get("count"))

    big = [json.dumps({"n": i, "pad": "x" * 200}) for i in range(3500)]
    log_file.write_text("\n".join(big) + "\n", encoding="utf-8")
    tail = routes_rail.tail_lines(log_file)
    check("drafts: reads only the last 3000 lines", len(tail) == 3000 and tail == big[-3000:], (len(tail), tail[:1]))

    obs.TOOLS_LOG = log_file.parent / "missing.jsonl"
    r = client.get("/drafts/recent")
    check("drafts: no log yet -> 200 and empty", r.status_code == 200 and r.json().get("count") == 0 and r.json().get("drafts") == [], r.text[:300])
    obs.TOOLS_LOG = log_file.parent  # a directory: unreadable as a file
    r = client.get("/drafts/recent")
    check("drafts: unreadable log -> 200 and empty", r.status_code == 200 and r.json().get("count") == 0, r.text[:300])
    for sid in ("s2", "s3", "s4", "s8"):
        web.SESSIONS.pop(sid, None)


def gate_checks(client: TestClient):
    os.environ["VAANI_AUTH"] = "google"
    try:
        codes = [client.get(p).status_code for p in ("/meetings/today", "/drafts/recent")]
    finally:
        os.environ["VAANI_AUTH"] = "single"
    check("gate: both routes need sign-in in google mode", codes == [401, 401], codes)


def main() -> int:
    setup_memory()
    client = TestClient(web.app)  # no lifespan: no Ollama check, no voice warm-up
    meetings_checks(client)
    drafts_checks(client)
    gate_checks(client)
    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:400]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} rail checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
