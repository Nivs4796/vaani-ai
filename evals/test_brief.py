"""Offline checks for the opening brief: groups, order, the worth-knowing rule, dedup,
empty texts, the stamp, the per-user cache and the error path.

No model, no network: tools.list_recent_emails is replaced by canned results (and, for
one end-to-end check, runs for real against a fake Gmail); memory is a temp file.

Run: .venv/bin/python -m evals.test_brief   (exit 1 on any failure)
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from assistant import brief, context, followups, google_auth, memory, obs, tools

CHECKS: list[tuple[str, bool, str]] = []
OWN = "me@example.test"
KEYS = ["needs_reply", "follow_ups", "worth_knowing", "updates", "promotions"]


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


def row(mid, sender, subject, group, unread=False, important=None, date="2026-10-12 17:42", snippet="hello"):
    r = {"id": mid, "from": f"{sender} <x@example.test>", "sender": sender, "subject": subject, "date": date,
         "unread": unread, "group": group, "kind": "person" if group == "needs_reply" else "automated", "snippet": snippet}
    if important is not None:
        r["important"] = important
    return r


def inbox(rows, unread=7, more=False) -> str:
    return json.dumps({"query": "in:inbox newer_than:3d", "count": len(rows), "unread_count": unread, "unread_count_note": "exact",
                       "more_available": more, "messages": rows})


ROWS = [
    row("n1", "Ravi Mehta (Basecamp)", "Re: Design package - request for quote", "needs_reply", unread=True),
    row("n2", "Priya Shah", "Lunch?", "needs_reply"),
    row("n3", "Priya Shah", "Lunch?", "needs_reply", unread=True),  # duplicate of n2
    row("u1", "Google", "Security alert for your account", "updates", unread=True),
    row("u2", "Stripe", "Your receipt from Acme", "updates"),
    row("u3", "Jira", "Sprint report", "updates", important=True),
    row("u4", "Basecamp (Acme Studio )", "Basecamp (Acme Studio ): Here's the latest activity", "updates", unread=True),
    row("u5", "Basecamp (Acme Studio )", "Basecamp (Acme Studio ): Here's the latest activity", "updates"),
    row("u6", "LinkedIn", "Jordan viewed your profile", "updates"),
    row("u7", "GitHub", "Weekly digest", "updates"),
    row("u8", "Medium", "Top stories", "updates"),
    row("u9", "Priya Shah", "Lunch?", "updates"),  # same sender+subject as a needs_reply entry
    row("p1", "Cloudflare", "Cloudflare Connect is almost sold out.", "promotions", unread=True, important=True),
    row("p2", "Atlassian", "Tell us about your experience", "promotions"),
    row("p3", "Atlassian", "Tell us about your experience", "promotions"),
]


class Fake:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, max_results=10, query=""):
        self.calls.append({"max_results": max_results, "query": query, "user": context.user_id()})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def setup_memory(extra=None) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "memory.json"
    data = copy.deepcopy(memory.DEFAULT)
    data.update(extra or {})
    tmp.write_text(json.dumps(data))
    memory.MEMORY_FILE = tmp
    return tmp


def fresh(result, **env):
    brief._cache.clear()
    for k in ("VAANI_BRIEF_DAYS", "VAANI_BRIEF_TTL_MIN", "VAANI_FOLLOWUPS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    fake = Fake(result)
    tools.list_recent_emails = fake
    return fake


def groups(b) -> dict:
    return {g["key"]: g for g in b.get("groups", [])}


# --------------------------------------------------------------------------- fake Gmail for the end-to-end check


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class Batch:
    def __init__(self, callback):
        self.callback, self.items = callback, []

    def add(self, req, request_id=None):
        self.items.append((request_id, req))

    def execute(self):
        for rid, req in self.items:
            self.callback(rid, req.execute(), None)


class FakeGmail:
    def __init__(self, msgs):
        self.msgs = msgs

    def users(self):
        return self

    def messages(self):
        return self

    def new_batch_http_request(self, callback=None):
        return Batch(callback)

    def list(self, userId="me", q="", maxResults=50):
        def run():
            out = [{"id": m["id"], "threadId": m["threadId"]} for m in self.msgs.values()
                   if "INBOX" in m["labelIds"] and ("is:unread" not in q or "UNREAD" in m["labelIds"])]
            return {"messages": out[:maxResults]}
        return Req(run)

    def get(self, userId="me", id="", format="metadata", metadataHeaders=None):
        return Req(lambda: copy.deepcopy(self.msgs[id]))


def gmsg(mid, frm, subject, labels, extra=None):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}, {"name": "Date", "value": "Mon, 12 Oct 2026 17:42:00 +0530"}]
    headers += [{"name": k, "value": v} for k, v in (extra or {}).items()]
    return {"id": mid, "threadId": f"t-{mid}", "labelIds": labels, "snippet": "hi", "payload": {"headers": headers}}


# --------------------------------------------------------------------------- checks


def main() -> int:
    real_list = tools.list_recent_emails
    tmpdir = Path(tempfile.mkdtemp())
    if hasattr(obs, "LOG_DIR"):
        obs.LOG_DIR, obs.TOOLS_LOG = tmpdir, tmpdir / "tools.jsonl"  # keep test rows out of logs/tools.jsonl
        if hasattr(obs, "TURNS_LOG"):
            obs.TURNS_LOG = tmpdir / "turns.jsonl"
    setup_memory()

    def temp_load(user_id=None):
        # Every user reads the temp file, so per-user checks never touch data/users/<id>/.
        data = copy.deepcopy(memory.DEFAULT)
        data.update(json.loads(memory.MEMORY_FILE.read_text()))
        return data

    memory.load = temp_load

    # ---- groups, order, rules
    raw = inbox(ROWS)
    fake = fresh(raw)
    b = brief.build()
    gs = groups(b)
    check("no error", "error" not in b, b.get("error"))
    check("five groups in order", [g["key"] for g in b["groups"]] == KEYS, [g["key"] for g in b["groups"]])
    check("titles", [g["title"] for g in b["groups"]] == ["Needs a reply", "Follow-ups", "Worth knowing", "Updates", "Promotions"])
    check("every group has an empty_text", all(isinstance(g.get("empty_text"), str) and g["empty_text"] for g in b["groups"]))
    check("every group has count and rows", all(isinstance(g.get("count"), int) and isinstance(g.get("rows"), list) for g in b["groups"]))
    check("query uses the brief window", fake.calls == [{"max_results": 20, "query": "in:inbox newer_than:3d", "user": None}], fake.calls)
    nr = gs["needs_reply"]
    check("needs_reply dedups sender+subject", nr["count"] == 2 and len(nr["rows"]) == 2, nr)
    lunch = next((r for r in nr["rows"] if r["subject"] == "Lunch?"), {})
    check("dedup keeps the newest id, counts, ORs unread", lunch.get("id") == "n2" and lunch.get("n") == 2 and lunch.get("unread") is True, lunch)
    check("needs_reply rows have the reply action", all(r["actions"] == ["reply"] for r in nr["rows"]), nr["rows"])
    check("row shape", all({"id", "sender", "subject", "date", "unread", "snippet", "actions"} <= set(r) for r in nr["rows"]), nr["rows"])
    wk = gs["worth_knowing"]
    wk_ids = [r["id"] for r in wk["rows"]]
    check("worth_knowing: keyword subjects and important updates", wk_ids == ["u1", "u2", "u3"], wk_ids)
    check("worth_knowing rows have the open action", all(r["actions"] == ["open"] for r in wk["rows"]), wk["rows"])
    check("worth_knowing never repeats a needs_reply entry", "u9" not in wk_ids and "u9" not in [r["id"] for r in gs["updates"]["rows"]])
    check("important promotions stay promotions", "p1" not in wk_ids)
    up = gs["updates"]
    check("updates: the rest, count + top 3", up["count"] == 4 and [r["id"] for r in up["rows"]] == ["u4", "u6", "u7"], up)
    check("updates dedup with a count", up["rows"][0].get("n") == 2 and up["rows"][0]["subject"] == "Here's the latest activity", up["rows"][0])
    pr = gs["promotions"]
    check("promotions: count only, archive_all", pr["rows"] == [] and pr["count"] == 2 and pr.get("messages") == 3 and pr.get("action") == "archive_all", pr)
    fu = gs["follow_ups"]
    check("follow-ups off by default", fu["rows"] == [] and fu["count"] == 0 and fu["empty_text"] == "Follow-ups are off", fu)

    # ---- stamp
    try:
        built = datetime.fromisoformat(b["built_at"])
        check("built_at is ISO with a timezone", built.tzinfo is not None, b["built_at"])
    except (KeyError, ValueError) as e:
        check("built_at is ISO with a timezone", False, repr(e))
    check("covers_days default 3", b["covers_days"] == 3, b.get("covers_days"))
    check("count is the messages listed", b["count"] == len(ROWS), b.get("count"))
    check("unread is the true unread count", b["unread"] == 7, b.get("unread"))
    check("tool_result is the raw tool string", b["tool_result"] == raw)

    # ---- markdown, same voice as agent.format_inbox_brief
    t = b["text"]
    check("text head", t.startswith("Inbox, last 3 days: 7 unread."), t[:80])
    check("text groups in order", t.index("**Needs a reply**") < t.index("**Worth knowing**") < t.index("**Updates**") < t.index("**Promotions**"), t)
    check("text dedup marker", "Priya Shah - Lunch? (x2, unread, 2026-10-12 17:42)" in t, t)
    check("text strips a sender prefix", "Basecamp (Acme Studio) - Here's the latest activity (x2, unread)" in t, t)
    check("text: more updates hidden", "... and 1 more" in t, t)
    check("text: promotions as a count", "3 promotional emails" in t, t)
    check("text suggests a follow-up", t.rstrip().endswith('"What does Ravi want?"'), t[-80:])
    check("text leaves follow-ups out when off", "**Follow-ups**" not in t)

    # ---- limits: up to 8 needs_reply rows, up to 5 worth_knowing rows
    many = [row(f"m{i}", f"Person {i}", f"Question {i}", "needs_reply") for i in range(10)]
    many += [row(f"w{i}", "Bank", f"Statement {i} is ready", "updates") for i in range(7)]
    b2 = (fresh(inbox(many, more=True)), brief.build())[1]
    g2 = groups(b2)
    check("needs_reply capped at 8 rows, count all", len(g2["needs_reply"]["rows"]) == 8 and g2["needs_reply"]["count"] == 10, g2["needs_reply"]["count"])
    check("worth_knowing capped at 5 rows", len(g2["worth_knowing"]["rows"]) == 5 and g2["worth_knowing"]["count"] == 7)
    check("text notes the hidden rows", "... and 2 more" in b2["text"] and "Showing the newest 17." in b2["text"], b2["text"][:200])

    # ---- follow-ups on
    fake_rows = [{"thread_id": "t1", "message_id": "s1", "to_name": "Sam Lee", "to_email": "sam@client.example", "subject": "Contract draft",
                  "days_waiting": 4, "snippet": "Here it is", "expects_reply": False, "sender": "Sam Lee", "date": "2026-10-09 10:00"}]
    real_candidates = followups.candidates
    seen = {}

    def cands(user_id=None, wait_days=None, limit=5):
        seen.update(user_id=user_id, limit=limit)
        return copy.deepcopy(fake_rows)

    followups.candidates = cands
    b3 = (fresh(raw, VAANI_FOLLOWUPS="1"), brief.build())[1]
    fu3 = groups(b3)["follow_ups"]
    r3 = (fu3["rows"] or [{}])[0]
    check("follow-ups on: rows come from candidates", fu3["count"] == 1 and r3.get("thread_id") == "t1" and r3.get("id") == "s1", fu3)
    check("follow-up rows: nudge and dismiss, days_waiting", r3.get("actions") == ["nudge", "dismiss"] and r3.get("days_waiting") == 4, r3)
    check("follow-ups asked for at most 5", seen.get("limit") == 5, seen)
    check("text lists follow-ups with the wait", "**Follow-ups**\n1. Sam Lee - Contract draft (waiting 4 days)" in b3["text"], b3["text"])
    fake_rows.clear()
    b4 = (fresh(raw, VAANI_FOLLOWUPS="1"), brief.build())[1]
    check("follow-ups on but none: empty_text", groups(b4)["follow_ups"]["empty_text"] == "No follow-ups waiting")
    followups.candidates = real_candidates

    # ---- empty inbox
    b5 = (fresh(inbox([], unread=0)), brief.build())[1]
    check("empty inbox text", b5["text"] == "Nothing in your inbox from the last 3 days.", b5["text"])
    check("empty inbox keeps five groups with zero counts", [g["key"] for g in b5["groups"]] == KEYS and all(g["count"] == 0 for g in b5["groups"]))
    check("empty needs_reply text", groups(b5)["needs_reply"]["empty_text"] == "Nothing from a person in the last 3 days.")

    # ---- days: VAANI_BRIEF_DAYS when set, then memory
    fake = fresh(raw, VAANI_BRIEF_DAYS="5")
    memory.load = lambda user_id=None: {"timezone": "Asia/Kolkata"}  # an old memory file without brief.days
    b6 = brief.build()
    memory.load = temp_load
    check("VAANI_BRIEF_DAYS when memory has no brief.days", b6["covers_days"] == 5 and fake.calls[0]["query"] == "in:inbox newer_than:5d"
          and b6["text"].startswith("Inbox, last 5 days"), (b6["covers_days"], fake.calls))
    setup_memory({"brief": {"days": 2}})
    fake = fresh(raw, VAANI_BRIEF_DAYS="5")
    b7 = brief.build()
    check("VAANI_BRIEF_DAYS overrides memory (demo-day switch)", b7["covers_days"] == 5 and fake.calls[0]["query"] == "in:inbox newer_than:5d", fake.calls)
    fake = fresh(raw)
    b7 = brief.build()
    check("memory brief.days when the env var is unset", b7["covers_days"] == 2 and fake.calls[0]["query"] == "in:inbox newer_than:2d", fake.calls)
    setup_memory()

    # ---- cache
    fake = fresh(raw)
    a = brief.build()
    a["groups"].clear()  # the caller mutating its copy must not touch the cache
    b8 = brief.build()
    check("cache hit within TTL", len(fake.calls) == 1 and len(b8["groups"]) == 5, len(fake.calls))
    brief.build(refresh=True)
    check("refresh bypasses the cache", len(fake.calls) == 2, len(fake.calls))
    brief.build(user_id="u-1")
    check("cache is per user", len(fake.calls) == 3 and fake.calls[-1]["user"] == "u-1", fake.calls[-1])
    brief.build(user_id="u-1")
    check("per-user cache hit", len(fake.calls) == 3)
    tok = context.current_user.set({"id": "u-1", "email": "u1@example.test"})
    try:
        brief.build()
        check("context user shares the cache with an explicit id", len(fake.calls) == 3)
    finally:
        context.current_user.reset(tok)
    brief.invalidate()
    brief.build()
    check("invalidate drops the cache", len(fake.calls) == 4)
    fake = fresh(raw, VAANI_BRIEF_TTL_MIN="0")
    brief.build()
    brief.build()
    check("TTL 0 rebuilds every time", len(fake.calls) == 2, len(fake.calls))

    # ---- error path: never raises, error dict, not cached
    for label, result in (("tool ERROR string", "ERROR: could not list emails (RefreshError: x); nothing was changed; check the Google connection"),
                          ("tool raises", RuntimeError("boom")), ("bad JSON", "{not json")):
        fake = fresh(result)
        try:
            e = brief.build()
            ok = isinstance(e.get("error"), str) and e["error"] and e["groups"] == [] and e["covers_days"] == 3 and "built_at" in e
            check(f"error path ({label}) returns an error dict", ok, e)
            brief.build()
            check(f"error path ({label}) is not cached", len(fake.calls) == 2, len(fake.calls))
        except Exception as ex:  # noqa: BLE001
            check(f"error path ({label}) returns an error dict", False, repr(ex))
    real_load = memory.load
    memory.load = lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    try:
        e = (fresh(raw), brief.build())[1]
        check("unreadable memory still builds", "error" not in e and e["covers_days"] == 3, e.get("error"))
    except Exception as ex:  # noqa: BLE001
        check("unreadable memory still builds", False, repr(ex))
    memory.load = real_load

    # ---- end to end: the real list_recent_emails against a fake Gmail
    tools.list_recent_emails = real_list
    brief._cache.clear()
    os.environ.pop("VAANI_FOLLOWUPS", None)
    os.environ.pop("VAANI_BRIEF_TTL_MIN", None)
    g = FakeGmail({
        "m1": gmsg("m1", "Priya Shah <priya@acme.example>", "Can we meet Thursday?", ["INBOX", "UNREAD", "CATEGORY_PERSONAL"]),
        "m2": gmsg("m2", "Google <no-reply@accounts.google.com>", "Security alert", ["INBOX", "CATEGORY_UPDATES"]),
        "m3": gmsg("m3", "GitHub <noreply@github.com>", "Weekly digest", ["INBOX", "CATEGORY_UPDATES"]),
        "m4": gmsg("m4", "Cloudflare <em@em1.cloudflare.com>", "Sale", ["INBOX", "CATEGORY_PROMOTIONS"], {"List-Unsubscribe": "<mailto:u@x>"}),
    })
    real_services = google_auth.get_services
    google_auth.get_services = lambda user_id=None: {"gmail": g, "calendar": None}
    try:
        e2e = brief.build(refresh=True)
        ge = groups(e2e)
        ok = ("error" not in e2e and [r["id"] for r in ge["needs_reply"]["rows"]] == ["m1"] and [r["id"] for r in ge["worth_knowing"]["rows"]] == ["m2"]
              and [r["id"] for r in ge["updates"]["rows"]] == ["m3"] and ge["promotions"]["messages"] == 1 and e2e["unread"] == 1)
        check("end to end through tools.list_recent_emails", ok, {k: [r["id"] for r in v["rows"]] for k, v in ge.items()} or e2e.get("error"))
        check("end to end tool_result parses", json.loads(e2e.get("tool_result") or "{}").get("count") == 4)
    finally:
        google_auth.get_services = real_services

    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:300]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} brief checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
