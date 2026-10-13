"""Offline checks for the follow-up detector, against a fake Gmail (sent list, threads, batch).

No model, no network: google_auth.get_services() and connected_email() are replaced by
fakes and data/memory.json by a temp file. Proves which threads count as "waiting on
someone" (own message last, sent to a person, not a list or no-reply, not dismissed),
the ranking, the batched read and the nudge template.

Run: .venv/bin/python -m evals.test_followups   (exit 1 on any failure)
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from assistant import followups, google_auth, memory

CHECKS: list[tuple[str, bool, str]] = []
OWN = "me@example.test"
DAY_MS = 86_400_000


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


# --------------------------------------------------------------------------- fakes


class Req:
    def __init__(self, fn, on_single=None):
        self.fn, self.on_single = fn, on_single

    def execute(self, num_retries=0):
        if self.on_single:
            self.on_single()  # a direct call, outside any batch
        return self.fn()


class Batch:
    def __init__(self, g, callback):
        self.g, self.callback, self.items = g, callback, []

    def add(self, req, request_id=None):
        self.items.append((request_id, req))

    def execute(self):
        self.g.batches.append(len(self.items))
        if self.g.batch_explodes:
            raise ConnectionError("batch endpoint down")
        for rid, req in self.items:
            try:
                if rid in self.g.fail_in_batch:
                    raise TimeoutError("one item failed")
                self.callback(rid, req.fn(), None)
            except Exception as e:  # noqa: BLE001
                self.callback(rid, None, e)


def msg(mid, tid, frm, to, subject, days_ago, snippet="", cc="", labels=("SENT",), extra=None):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": "Mon, 12 Oct 2026 17:42:00 +0530"}, {"name": "Message-ID", "value": f"<{mid}@mail.example>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    for k, v in (extra or {}).items():
        headers.append({"name": k, "value": v})
    return {"id": mid, "threadId": tid, "labelIds": list(labels), "snippet": snippet,
            "internalDate": str(int(time.time() * 1000 - days_ago * DAY_MS)), "payload": {"headers": headers}}


class FakeGmail:
    def __init__(self):
        self.threads_store: dict[str, list[dict]] = {}
        self.queries: list[tuple[str, int]] = []
        self.single_gets: list[str] = []
        self.batches: list[int] = []
        self.fail_in_batch: set[str] = set()
        self.batch_explodes = False

    def users(self):
        return self

    def messages(self):
        return _Messages(self)

    def threads(self):
        return _Threads(self)

    def new_batch_http_request(self, callback=None):
        return Batch(self, callback)

    def add(self, *messages):
        for m in messages:
            self.threads_store.setdefault(m["threadId"], []).append(m)


class _Messages:
    def __init__(self, g):
        self.g = g

    def list(self, userId="me", q="", maxResults=100):
        def run():
            self.g.queries.append((q, maxResults))
            newer = int(re.search(r"newer_than:(\d+)d", q).group(1))
            older = int(re.search(r"older_than:(\d+)d", q).group(1))
            now = time.time() * 1000
            out = []
            for msgs in self.g.threads_store.values():
                for m in msgs:
                    age = now - int(m["internalDate"])
                    if "SENT" in m["labelIds"] and older * DAY_MS <= age <= newer * DAY_MS:
                        out.append({"id": m["id"], "threadId": m["threadId"]})
            return {"messages": out[:maxResults]}
        return Req(run)


class _Threads:
    def __init__(self, g):
        self.g = g

    def get(self, userId="me", id="", format="full", metadataHeaders=None):
        def run():
            assert format == "metadata" and "List-Unsubscribe" in (metadataHeaders or []), (format, metadataHeaders)
            return {"id": id, "messages": copy.deepcopy(self.g.threads_store[id])}
        return Req(run, on_single=lambda: self.g.single_gets.append(id))


ME = f"Alex Kim <{OWN}>"


def seed(g: FakeGmail) -> None:
    g.add(
        # kept: own message last, to a person, asks a question (5 days)
        msg("a1", "t-ask", ME, "Priya Shah <priya@acme.example>", "Re: Deck for Friday", 5, "Could you send the final deck?"),
        # kept: a conversation (Sam asked first), own message last, no question, waiting longest (7 days)
        msg("b0", "t-fyi", "Sam Lee <sam@client.example>", OWN, "Contract draft", 9, "Can you share the contract draft?", labels=("INBOX",)),
        msg("b1", "t-fyi", ME, "Sam Lee <sam@client.example>", "Re: Contract draft", 7, "Here is the contract draft."),
        # dropped: a one-off send that asks nothing (an app notice sent from this account)
        msg("n1", "t-notice", ME, "Dana Cole <dana@client.example>", "Your trial ends soon", 6, "Your free trial ends on Friday."),
        # dropped: the person replied after the user
        msg("c1", "t-replied", ME, "Ravi Mehta <ravi@studio.example>", "Quote", 6, "Can you confirm the quote?"),
        msg("c2", "t-replied", "Ravi Mehta <ravi@studio.example>", OWN, "Re: Quote", 2, "Confirmed, thanks", labels=("INBOX",)),
        # dropped: only a no-reply address
        msg("d1", "t-noreply", ME, "Billing <no-reply@service.example>", "Refund?", 5, "Can you refund this?"),
        # dropped: a mailing list address
        msg("e1", "t-list", ME, "Team <eng-team@googlegroups.com>", "Weekly notes", 5, "Thoughts welcome?"),
        # dropped: the user's message carries List-Unsubscribe (sent through a bulk tool)
        msg("f1", "t-bulk", ME, "Jordan <jordan@client.example>", "Newsletter", 5, "Let me know", extra={"List-Unsubscribe": "<mailto:u@x>"}),
        # dropped: dismissed earlier
        msg("g1", "t-dismissed", ME, "Lee Wong <lee@client.example>", "Old ask", 5, "Can you check?"),
        # dropped: auto-submitted (vacation reply)
        msg("h1", "t-auto", ME, "Kim <kim@client.example>", "Out of office", 5, "I am away", extra={"Auto-Submitted": "auto-replied"}),
        # dropped: a note to self
        msg("i1", "t-self", ME, ME, "Reminder", 5, "Remember the thing?"),
        # kept: To is a no-reply address but a person is on Cc; "let me know" (4 days)
        msg("j1", "t-cc", ME, "notifications@tool.example", "Access request", 4, "Please let me know once it's done.", cc="Maya Iyer <maya@acme.example>"),
        # thread whose newest real message is the user's, followed by an unsent draft (kept, draft ignored)
        msg("k0", "t-draft", "Omar Ali <omar@vendor.example>", OWN, "Invoice", 12, "Can you resend the invoice?", labels=("INBOX",)),
        msg("k1", "t-draft", ME, "Omar Ali <omar@vendor.example>", "Fwd: Invoice", 9, "Please find the invoice attached."),
        msg("k2", "t-draft", ME, "Omar Ali <omar@vendor.example>", "Re: Invoice", 0, "draft text", labels=("DRAFT",)),
        # dropped by wait: an old message matched the query but the user wrote again yesterday
        msg("l1", "t-recent", ME, "Nia Roy <nia@client.example>", "Plan", 6, "First pass attached"),
        msg("l2", "t-recent", ME, "Nia Roy <nia@client.example>", "Re: Plan", 1, "Any update on this?"),
    )


def setup() -> FakeGmail:
    tmp = Path(tempfile.mkdtemp()) / "memory.json"
    data = copy.deepcopy(memory.DEFAULT)
    data["followups"] = {"wait_days": 3, "dismissed": ["t-dismissed"]}
    tmp.write_text(json.dumps(data))
    memory.MEMORY_FILE = tmp
    g = FakeGmail()
    seed(g)
    google_auth.get_services = lambda user_id=None: {"gmail": g, "calendar": None}
    google_auth.connected_email = lambda user_id=None: OWN
    followups._own_cache.clear()
    return g


# --------------------------------------------------------------------------- checks


def main() -> int:
    g = setup()
    rows = followups.candidates()
    ids = [r["thread_id"] for r in rows]
    check("query is sent mail 14d back, older than wait", g.queries and g.queries[0] == ("in:sent newer_than:14d older_than:3d", 50), g.queries)
    # 12 threads have a sent message in the window; the dismissed one is skipped before the read
    check("threads are read in one batch, dismissed one skipped", g.batches == [len(g.threads_store) - 1] and not g.single_gets,
          (g.batches, g.single_gets))
    check("own-last thread kept", "t-ask" in ids and "t-fyi" in ids, ids)
    check("replied thread dropped", "t-replied" not in ids, ids)
    check("no-reply recipient dropped", "t-noreply" not in ids, ids)
    check("list recipient dropped", "t-list" not in ids, ids)
    check("List-Unsubscribe on own message dropped", "t-bulk" not in ids, ids)
    check("dismissed thread dropped", "t-dismissed" not in ids, ids)
    check("auto-submitted dropped", "t-auto" not in ids, ids)
    check("note to self dropped", "t-self" not in ids, ids)
    check("one-off send that asks nothing dropped", "t-notice" not in ids, ids)
    check("a person on Cc is enough", "t-cc" in ids, ids)
    check("a trailing draft is ignored", "t-draft" in ids, ids)
    check("wrote again inside the wait: dropped", "t-recent" not in ids, ids)
    check("ranking: questions first, then longest wait", ids == ["t-ask", "t-cc", "t-draft", "t-fyi"], ids)
    by = {r["thread_id"]: r for r in rows}
    ask, cc, draft = by.get("t-ask", {}), by.get("t-cc", {}), by.get("t-draft", {})
    check("row shape", set(ask) >= {"thread_id", "message_id", "to_name", "to_email", "subject", "days_waiting", "snippet",
                                    "expects_reply", "sender", "date"}, sorted(ask))
    check("row fields", ask.get("message_id") == "a1" and ask.get("to_name") == "Priya Shah" and ask.get("to_email") == "priya@acme.example"
          and ask.get("sender") == "Priya Shah" and ask.get("days_waiting") == 5 and ask.get("expects_reply") is True
          and ask.get("subject") == "Re: Deck for Friday" and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", ask.get("date", "")), ask)
    check("Cc person is the addressee", cc.get("to_email") == "maya@acme.example" and cc.get("expects_reply") is True, cc)
    check("message_id is the user's last sent message, not the draft", draft.get("message_id") == "k1" and draft.get("days_waiting") == 9, draft)
    check("plain note does not expect a reply", by.get("t-fyi", {}).get("expects_reply") is False, by.get("t-fyi"))

    check("limit applies", len(followups.candidates(limit=2)) == 2)
    g.queries.clear()
    short = [r["thread_id"] for r in followups.candidates(wait_days=1, limit=10)]
    check("wait_days argument wins", g.queries[0][0] == "in:sent newer_than:14d older_than:1d" and "t-recent" in short, (g.queries, short))
    data = memory.load()
    data["followups"]["wait_days"] = 6
    memory.save(data)
    g.queries.clear()
    six = [r["thread_id"] for r in followups.candidates(limit=10)]
    check("memory wait_days is used", g.queries[0][0].endswith("older_than:6d") and six == ["t-draft", "t-fyi"], (g.queries, six))
    data["followups"]["wait_days"] = 3
    memory.save(data)

    # dismiss persists in memory and hides the thread; repeated dismiss does not duplicate
    check("dismiss returns True", followups.dismiss("t-ask") is True)
    followups.dismiss("t-ask")
    dismissed = memory.load()["followups"]["dismissed"]
    check("dismiss is stored once", dismissed.count("t-ask") == 1 and "t-dismissed" in dismissed, dismissed)
    check("dismissed thread disappears", "t-ask" not in [r["thread_id"] for r in followups.candidates(limit=10)])
    check("empty thread id is refused", followups.dismiss("") is False)

    # batch failures fall back to single gets; total failure is an empty list, never a raise
    g.fail_in_batch = {"t-fyi"}
    g.single_gets.clear()
    again = [r["thread_id"] for r in followups.candidates(limit=10)]
    check("an item failing in the batch is retried alone", "t-fyi" in again and "t-fyi" in g.single_gets, (again, g.single_gets))
    g.fail_in_batch = set()
    g.batch_explodes = True
    exploded = [r["thread_id"] for r in followups.candidates(limit=10)]
    check("whole batch failing falls back to single gets", "t-fyi" in exploded and "t-cc" in exploded, exploded)
    g.batch_explodes = False
    real = google_auth.get_services

    def boom(user_id=None):
        raise RuntimeError("token revoked")

    google_auth.get_services = boom
    try:
        out = followups.candidates()
        check("Gmail failure returns an empty list", out == [], out)
    except Exception as e:  # noqa: BLE001
        check("Gmail failure returns an empty list", False, repr(e))
    google_auth.get_services = real
    followups._own_cache.clear()
    google_auth.connected_email = lambda user_id=None: (_ for _ in ()).throw(RuntimeError("no profile"))
    check("unknown own address returns an empty list", followups.candidates() == [])
    google_auth.connected_email = lambda user_id=None: OWN

    # nudge template
    body = followups.nudge_body({"to_name": "Priya Shah", "subject": "Re: Fwd: Deck for Friday", "expects_reply": True}, "Alex Kim")
    check("nudge body for a question", body == "Hi Priya, just following up on my note about Deck for Friday. "
          "Could you let me know when you get a chance?\n\nThanks,\nAlex", body)
    body2 = followups.nudge_body({"to_name": "Sam Lee", "subject": "Contract draft", "expects_reply": False}, "Alex")
    check("nudge body for a plain note", body2 == "Hi Sam, just following up on my note about Contract draft. "
          "Let me know if you need anything from me.\n\nThanks,\nAlex", body2)
    body3 = followups.nudge_body({"to_name": "sam.lee@client.example", "subject": "FW: Plan", "expects_reply": False}, "Alex")
    check("first name from a bare address", body3.startswith("Hi Sam, just following up on my note about Plan."), body3)

    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:300]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} followups checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
