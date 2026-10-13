"""Offline checks for the Gmail and Calendar write actions, against a fake Google API.

No model, no network, no real mailbox or calendar: google_auth.get_services() is
replaced by in-memory fakes, and data/memory.json by a temp file. This is where the
safety rules are proven: nothing is sent or changed without the Confirm click, a
send never happens twice, recipients come only from typed or saved addresses (or the
sender being replied to), and every change is read back.

Run: .venv/bin/python -m evals.test_manage   (exit 1 on any failure)
"""

from __future__ import annotations

import base64
import copy
import email
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from assistant import agent, google_auth, manage, memory, obs, tools

CHECKS: list[tuple[str, bool, str]] = []
OWN = "me@example.test"


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


# --------------------------------------------------------------------------- fakes


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
            try:
                self.callback(rid, req.execute(), None)
            except Exception as e:  # noqa: BLE001
                self.callback(rid, None, e)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def fake_message(mid, frm, subject, labels, body="Hello", extra_headers=None, thread="t-" + "x"):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}, {"name": "To", "value": OWN},
               {"name": "Date", "value": "Mon, 12 Oct 2026 17:42:00 +0530"}, {"name": "Message-ID", "value": f"<{mid}@mail.example>"}]
    for k, v in (extra_headers or {}).items():
        headers.append({"name": k, "value": v})
    return {"id": mid, "threadId": f"thread-{mid}", "labelIds": list(labels), "snippet": body[:40],
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64(body)}}}


class FakeGmail:
    def __init__(self):
        self.msgs: dict[str, dict] = {}
        self.sent_raw: list[dict] = []
        self.drafts_made: list[dict] = []
        self.labels_store = [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_1", "name": "Clients"}]
        self.fail_send = False
        self.n = 0

    # resource navigation
    def users(self):
        return self

    def messages(self):
        return _Messages(self)

    def drafts(self):
        return _Drafts(self)

    def new_batch_http_request(self, callback=None):
        return Batch(callback)

    def getProfile(self, userId="me"):
        return Req(lambda: {"emailAddress": OWN})

    def _header(self, m, name):
        for h in m["payload"]["headers"]:
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""


FakeGmail.labels = lambda self: _Labels(self)  # gmail.users().labels()


class _Messages:
    def __init__(self, g):
        self.g = g

    def list(self, userId="me", q="", maxResults=50, includeSpamTrash=False):
        def run():
            out = []
            for m in self.g.msgs.values():
                labels = set(m["labelIds"])
                if "TRASH" in labels and not includeSpamTrash:
                    continue
                ok = True
                for tok in q.split():
                    if tok == "in:inbox" and "INBOX" not in labels:
                        ok = False
                    if tok == "is:unread" and "UNREAD" not in labels:
                        ok = False
                    if tok == "in:trash" and "TRASH" not in labels:
                        ok = False
                    if tok.startswith("from:") and tok[5:].strip('"').lower() not in self.g._header(m, "From").lower():
                        ok = False
                if ok:
                    out.append({"id": m["id"], "threadId": m["threadId"]})
            return {"messages": out[:maxResults]}
        return Req(run)

    def get(self, userId="me", id="", format="full", metadataHeaders=None):
        return Req(lambda: copy.deepcopy(self.g.msgs[id]))

    def send(self, userId="me", body=None):
        def run():
            if self.g.fail_send:
                raise TimeoutError("network timeout")
            self.g.n += 1
            mid = f"sent{self.g.n}"
            self.g.sent_raw.append(body)
            self.g.msgs[mid] = {"id": mid, "threadId": body.get("threadId", mid), "labelIds": ["SENT"], "snippet": "",
                                "payload": {"headers": [], "mimeType": "text/plain", "body": {}}}
            return {"id": mid, "threadId": body.get("threadId", mid)}
        return Req(run)

    def batchModify(self, userId="me", body=None):
        def run():
            for mid in body["ids"]:
                labels = self.g.msgs[mid]["labelIds"]
                for lab in body.get("removeLabelIds") or []:
                    if lab in labels:
                        labels.remove(lab)
                for lab in body.get("addLabelIds") or []:
                    if lab not in labels:
                        labels.append(lab)
            return {}
        return Req(run)

    def trash(self, userId="me", id=""):
        def run():
            labels = self.g.msgs[id]["labelIds"]
            if "TRASH" not in labels:
                labels.append("TRASH")
            if "INBOX" in labels:
                labels.remove("INBOX")
            return {"id": id}
        return Req(run)

    def untrash(self, userId="me", id=""):
        def run():
            labels = self.g.msgs[id]["labelIds"]
            if "TRASH" in labels:
                labels.remove("TRASH")
            labels.append("INBOX")
            return {"id": id}
        return Req(run)


class _Drafts:
    def __init__(self, g):
        self.g = g

    def create(self, userId="me", body=None):
        def run():
            self.g.n += 1
            mid = f"draftmsg{self.g.n}"
            self.g.drafts_made.append(body)
            self.g.msgs[mid] = {"id": mid, "threadId": mid, "labelIds": ["DRAFT"], "snippet": "",
                                "payload": {"headers": [], "mimeType": "text/plain", "body": {}}}
            return {"id": f"r{self.g.n}", "message": {"id": mid}}
        return Req(run)


class _Labels:
    def __init__(self, g):
        self.g = g

    def list(self, userId="me"):
        return Req(lambda: {"labels": list(self.g.labels_store)})

    def create(self, userId="me", body=None):
        def run():
            lab = {"id": f"Label_{len(self.g.labels_store) + 1}", "name": body["name"]}
            self.g.labels_store.append(lab)
            return lab
        return Req(run)


class FakeCal:
    def __init__(self):
        self.ev: dict[str, dict] = {}
        self.patches: list[tuple[str, dict]] = []

    def events(self):
        return self

    def list(self, calendarId="primary", timeMin=None, timeMax=None, singleEvents=True, orderBy="startTime", maxResults=50, q=None):
        def run():
            lo, hi = datetime.fromisoformat(timeMin), datetime.fromisoformat(timeMax)
            items = []
            for e in self.ev.values():
                s = datetime.fromisoformat(e["start"]["dateTime"])
                if lo <= s < hi:
                    items.append(copy.deepcopy(e))
            items.sort(key=lambda e: e["start"]["dateTime"])
            return {"items": items}
        return Req(run)

    def get(self, calendarId="primary", eventId=""):
        return Req(lambda: copy.deepcopy(self.ev[eventId]))

    def patch(self, calendarId="primary", eventId="", body=None, sendUpdates=None):
        def run():
            self.patches.append((eventId, body))
            self.ev[eventId].update(copy.deepcopy(body))
            return copy.deepcopy(self.ev[eventId])
        return Req(run)

    def delete(self, calendarId="primary", eventId="", sendUpdates=None):
        def run():
            self.ev[eventId]["status"] = "cancelled"
            return {}
        return Req(run)


# --------------------------------------------------------------------------- setup


def setup():
    tmp = Path(tempfile.mkdtemp()) / "memory.json"
    data = copy.deepcopy(memory.DEFAULT)
    tmp.write_text(json.dumps(data))
    memory.MEMORY_FILE = tmp
    tools._own_email = OWN

    g = FakeGmail()
    g.labels_store = [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_1", "name": "Clients"}]
    g.msgs = {
        "m1": fake_message("m1", '"Ravi Mehta (Basecamp)" <notifications@3.basecamp.com>', "Re: Design package - request for quote",
                           ["INBOX", "UNREAD", "CATEGORY_PERSONAL"], "Can you confirm the quote by Friday?",
                           {"Reply-To": "Basecamp <reply+abc123@3.basecamp.com>", "Precedence": "bulk"}),
        "m2": fake_message("m2", "Cloudflare <em@em1.cloudflare.com>", "Connect is almost sold out",
                           ["INBOX", "UNREAD", "CATEGORY_UPDATES"], "Sale", {"List-Unsubscribe": "<mailto:u@x>"}),
        "m3": fake_message("m3", "Atlassian <info@e.atlassian.com>", "Tell us about your experience",
                           ["INBOX", "CATEGORY_PROMOTIONS"], "Survey", {"List-Unsubscribe": "<mailto:u@x>"}),
        "m4": fake_message("m4", "Google Play <googleplay-noreply@google.com>", "Terms reminder", ["INBOX", "CATEGORY_UPDATES"], "Terms"),
    }
    tz = tools._tz()
    tmr = (datetime.now(tz) + timedelta(days=1)).date()

    def at(h, m=0):
        return datetime(tmr.year, tmr.month, tmr.day, h, m, tzinfo=tz).isoformat()

    c = FakeCal()
    c.ev = {
        "e1": {"id": "e1", "status": "confirmed", "summary": "Call with Andy Chen", "start": {"dateTime": at(10)}, "end": {"dateTime": at(11)},
               "attendees": [{"email": "andy.chen@example.test"}], "organizer": {"email": OWN, "self": True}, "htmlLink": "https://cal/e1"},
        "e2": {"id": "e2", "status": "confirmed", "summary": "Design review", "start": {"dateTime": at(14)}, "end": {"dateTime": at(15)},
               "organizer": {"email": "boss@acme.example"}, "attendees": [{"email": OWN, "self": True, "responseStatus": "needsAction"}],
               "htmlLink": "https://cal/e2"},
        "e3": {"id": "e3", "status": "confirmed", "summary": "Call with Andy Patel", "start": {"dateTime": at(16)}, "end": {"dateTime": at(16, 30)},
               "organizer": {"email": OWN, "self": True}, "htmlLink": "https://cal/e3"},
    }
    google_auth.get_services = lambda: {"gmail": g, "calendar": c}
    return g, c, tmr


def user(session, text):
    session.messages.append({"role": "user", "content": f"{agent._now_tag()} {text}"})


def j(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


# --------------------------------------------------------------------------- checks


def main() -> int:
    g, c, tmr = setup()
    s = agent.new_session()
    token = obs.current_session.set(s)
    try:
        # ---- email: new message, recipient rule, placeholders, nothing sent before Confirm
        user(s, "Email sam.lee@example.org that I will join the 4pm call")
        r = j(manage.draft_email(["sam.lee@example.org"], "Joining at 4", "Hi Alex,\nI'll join the 4pm call.\n[Your Name]"))
        d = s.drafts.get(r.get("draft_id"), {})
        check("draft_email prepares a card", r.get("status") == "READY_TO_CONFIRM" and d.get("kind") == "email_send", r)
        check("[Your Name] is filled from memory", d.get("body", "").endswith("Alex"), d.get("body"))
        check("nothing is sent at draft time", not g.sent_raw)
        bad = manage.draft_email(["stranger@evil.example"], "Hi", "Hello")
        check("an address the user never typed is refused", bad.startswith("ERROR:") and "recipient" in bad, bad[:160])
        r = j(manage.draft_email(["sam.lee@example.org"], "", "Hi Sam,\nThis is a Vaani UI test. Please ignore.\nAlex"))
        d = s.drafts.get(r.get("draft_id"), {})
        check("a missing subject comes from the body's first line (not the greeting)", d.get("subject") == "This is a Vaani UI test", d.get("subject"))
        r = j(manage.draft_email(["sam.lee@example.org"], "Joining at 4", "Hi Alex,\nI'll join the 4pm call.\n[Your Name]"))  # back to the card the checks below confirm
        d = s.drafts.get(r.get("draft_id"), {})

        # ---- the gate: commit refuses without the click
        res = manage.commit_pending(d["draft_id"])
        check("commit without the Confirm click is refused", res.startswith("NOT DONE:") and not g.sent_raw, res[:120])

        # ---- Confirm through the agent: sends once, verified, reported from Google's answer
        r2 = agent.run_turn(s, confirm_draft_id=d["draft_id"])
        sent = g.sent_raw[0] if g.sent_raw else {}
        raw = email.message_from_bytes(base64.urlsafe_b64decode(sent.get("raw", ""))) if sent else None
        check("Confirm sends exactly one email", len(g.sent_raw) == 1, len(g.sent_raw))
        check("sent message has the right headers", raw is not None and raw["To"] == "sam.lee@example.org" and raw["Subject"] == "Joining at 4" and raw["From"] == OWN, dict(raw.items()) if raw else None)
        check("reply to the user comes from the verified send", r2.reply.startswith("Sent and verified"), r2.reply)
        check("the commit is in the transcript", any(m.get("tool_name") == "commit_pending" for m in s.messages))
        r3 = agent.run_turn(s, confirm_draft_id=d["draft_id"])
        check("a second Confirm never sends twice", len(g.sent_raw) == 1 and "already carried out" in r3.reply, r3.reply)

        # ---- reply: threading headers, default recipient is the Reply-To of the original
        s2 = agent.new_session()
        obs.current_session.reset(token)
        token = obs.current_session.set(s2)
        user(s2, "Reply to Ravi saying the quote is approved")
        r = j(manage.draft_email(None, "", "Hi Ravi,\nThe quote is approved.\nAlex", reply_to_message_id="m1"))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("reply goes to the original Reply-To address", d.get("to") == ["reply+abc123@3.basecamp.com"], d.get("to"))
        check("reply keeps the subject and thread", d.get("subject") == "Re: Design package - request for quote" and d.get("thread_id") == "thread-m1"
              and d.get("in_reply_to") == "<m1@mail.example>", {k: d.get(k) for k in ("subject", "thread_id", "in_reply_to")})
        r = j(manage.draft_email(['Ravi Mehta (Basecamp) <notifications@3.basecamp.com>'], "", "Thanks", reply_to_message_id="m1"))
        dd = s2.drafts.get(r.get("draft_id"), {})
        check("a reply addressed to the sender goes to Reply-To", dd.get("to") == ["reply+abc123@3.basecamp.com"], r)
        lp = agent._Loop(s2)
        lp.email_hint = ("reply", "m1")
        check("the found email id is attached to a reply draft", lp._with_email_hint("draft_email", {"body": "x"}).get("reply_to_message_id") == "m1")
        check("an explicit id from the model is kept", lp._with_email_hint("draft_email", {"body": "x", "reply_to_message_id": "m9"})["reply_to_message_id"] == "m9")
        check("other tools are untouched", "reply_to_message_id" not in lp._with_email_hint("read_email", {"message_id": "m1"}))
        user(s2, "invite Pat <pat@acme.example> too")
        check("Name <addr> recipients parse", tools._coerce_attendees(["Pat <pat@acme.example>"], memory.load(), tools._typed_addresses(s2))[0] == ["pat@acme.example"])
        check("the older reply card is refused once replaced", "replaced by a newer one" in agent.run_turn(s2, confirm_draft_id=d["draft_id"]).reply)
        d = s2.drafts[s2.pending_draft_id]  # only the newest card can be confirmed
        # Save to drafts instead of sending
        r4 = agent.run_turn(s2, confirm_draft_id=d["draft_id"], confirm_variant="save_draft")
        check("Save to Gmail drafts creates a draft and sends nothing", len(g.drafts_made) == 1 and len(g.sent_raw) == 1 and "Saved to your Gmail drafts" in r4.reply, r4.reply)

        # ---- forward: original text included, attachments note
        user(s2, "Forward the Atlassian email to sam.lee@example.org")
        r = j(manage.draft_email(["sam.lee@example.org"], "", "FYI", forward_message_id="m3"))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("forward has Fwd: subject and the original text", d.get("subject", "").startswith("Fwd: ") and "Forwarded message" in d.get("body", "") and "Survey" in d.get("body", ""), d.get("subject"))
        check("forward card warns attachments are not forwarded", "Attachments are not forwarded" in d["card"]["note"])

        # ---- a failed send is never retried automatically
        user(s2, "Email sam.lee@example.org hello")
        r = j(manage.draft_email(["sam.lee@example.org"], "Hello", "Hello there"))
        did = r["draft_id"]
        g.fail_send = True
        r5 = agent.run_turn(s2, confirm_draft_id=did)
        g.fail_send = False
        r6 = agent.run_turn(s2, confirm_draft_id=did)
        check("a send that failed mid-way tells the user to check Sent", "Check your Sent folder" in r5.reply, r5.reply)
        check("and the same card can never send again", len(g.sent_raw) == 1 and "Sent" in r6.reply and not r6.reply.startswith("Sent and"), r6.reply)

        # ---- modify: group selection, nothing changes before Confirm, verified after
        r = j(manage.modify_emails("archive", group="promotions"))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("archive promotions picks only promotional mail", sorted(d.get("ids", [])) == ["m2", "m3"], d.get("ids"))
        check("nothing is archived before Confirm", "INBOX" in g.msgs["m2"]["labelIds"] and "INBOX" in g.msgs["m3"]["labelIds"])
        r7 = agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        check("Confirm archives and verifies", "INBOX" not in g.msgs["m2"]["labelIds"] and "Archived 2 of 2" in r7.reply, r7.reply)
        r = j(manage.modify_emails("mark_read", query="from:ravi is:unread"))
        d = s2.drafts.get(r.get("draft_id"), {})
        agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        check("mark read by Gmail query", "UNREAD" not in g.msgs["m1"]["labelIds"], g.msgs["m1"]["labelIds"])
        r = j(manage.modify_emails("trash", message_ids=["m4"]))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("trash card is marked dangerous and says nothing is permanently deleted", d["card"]["danger"] and "Nothing is permanently deleted" in d["card"]["note"], d["card"])
        agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        check("trash moves to Trash", "TRASH" in g.msgs["m4"]["labelIds"], g.msgs["m4"]["labelIds"])
        r = j(manage.modify_emails("restore", query="in:trash"))
        d = s2.drafts.get(r.get("draft_id"), {})
        agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        check("restore brings it back", "TRASH" not in g.msgs["m4"]["labelIds"] and "INBOX" in g.msgs["m4"]["labelIds"], g.msgs["m4"]["labelIds"])
        r = j(manage.modify_emails("label", message_ids=["m1"], label="Quotes"))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("a new label is only announced at draft time", not any(lb["name"] == "Quotes" for lb in g.labels_store) and "Creates the label" in d["card"]["note"])
        agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        check("and created on Confirm", any(lb["name"] == "Quotes" for lb in g.labels_store))
        check("unknown action is refused", manage.modify_emails("explode", message_ids=["m1"]).startswith("ERROR:"))
        for mid in ("m2", "m3"):  # the archive check above moved them out of the inbox; put them back
            if "INBOX" not in g.msgs[mid]["labelIds"]:
                g.msgs[mid]["labelIds"].append("INBOX")
        user(s2, "Archive all my promotional emails")
        raw = manage.modify_emails("unlabel", group="promotions", label="promotions")
        r = j(raw) or {"error": raw[:160]}
        d = s2.drafts.get(r.get("draft_id"), {})
        check("the user's 'archive' wins over the model's 'unlabel'", d.get("action") == "archive" and sorted(d.get("ids", [])) == ["m2", "m3"], r)
        user(s2, "Mark all promotions as read")
        r = j(manage.modify_emails("archive"))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("the user's group fills in missing targets", d.get("action") == "mark_read" and sorted(d.get("ids", [])) == ["m2", "m3"], r)
        user(s2, "Label the Cloudflare email as Clients")
        r = j(manage.modify_emails("label", message_ids=["m2"], label="Clients"))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("a plain label request is left alone", d.get("action") == "label" and d.get("label_name") == "Clients", r)
        s2.messages.append({"role": "user", "content": f"{agent._now_tag()} User clicked Move to Trash 1 on card dxxxx."})
        check("a button click is never read as the user's words", manage._user_said(s2) == (None, None), manage._user_said(s2))

        # ---- calendar: move, ambiguity, delete, RSVP, free time
        r = j(manage.draft_event_change("update", event_query="Andy Chen", on_date="tomorrow", new_start_iso=f"{tmr.isoformat()}T12:00"))
        d = s2.drafts.get(r.get("draft_id"), {})
        check("move prepares a card and keeps the length", d.get("kind") == "event_update" and d["patch"]["end"]["dateTime"][11:16] == "13:00", d.get("patch"))
        check("nothing moves before Confirm", c.ev["e1"]["start"]["dateTime"][11:16] == "10:00")
        r8 = agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        check("Confirm moves the event and verifies", c.ev["e1"]["start"]["dateTime"][11:16] == "12:00" and r8.reply.startswith("Updated and verified"), r8.reply)
        amb = manage.draft_event_change("delete", event_query="Andy", on_date="tomorrow")
        check("an ambiguous event asks which one, listing ids", amb.startswith("ERROR:") and "[e1]" in amb and "[e3]" in amb, amb[:200])
        r = j(manage.draft_event_change("delete", event_id="e3"))
        d = s2.drafts.get(r.get("draft_id"), {})
        agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        check("delete cancels the event", c.ev["e3"]["status"] == "cancelled")
        own_rsvp = manage.draft_event_change("rsvp", event_id="e1", rsvp="decline")
        check("RSVP to your own event is refused", own_rsvp.startswith("ERROR:") and "organises" in own_rsvp, own_rsvp[:120])
        r = j(manage.draft_event_change("rsvp", event_query="Design review", rsvp="accept"))
        d = s2.drafts.get(r.get("draft_id"), {})
        agent.run_turn(s2, confirm_draft_id=d["draft_id"])
        me = [a for a in c.ev["e2"]["attendees"] if a.get("self")][0]
        check("RSVP accept is recorded", me["responseStatus"] == "accepted", me)
        free = j(manage.find_free_time("tomorrow", 30))
        slots = [(x["from"], x["to"]) for x in free.get("free", [])]
        check("free time skips meetings in working hours", slots == [("09:00", "12:00"), ("13:00", "14:00"), ("15:00", "18:00")], slots)

        # ---- cancel wording per card kind
        r = j(manage.draft_email(["sam.lee@example.org"], "Later", "Text"))
        rc = agent.run_turn(s2, cancel_draft_id=r["draft_id"])
        check("cancelling an email says nothing was sent", rc.reply == "OK, nothing was sent.", rc.reply)
    finally:
        obs.current_session.reset(token)

    # ---- guards and routing (no Google, no model)
    for text, want in [
        ("Permanently delete the Cloudflare emails", agent.PERMA_DELETE_REFUSAL),
        ("Empty my trash", agent.PERMA_DELETE_REFUSAL),
        ("Can you set up a filter for Basecamp mail?", agent.SETTINGS_REFUSAL),
        ("Turn on my out of office reply", agent.SETTINGS_REFUSAL),
        ("Send Ravi the attached proposal", agent.ATTACH_REFUSAL),
        ("Email sam.lee@example.org the latest news about Claude", agent.WEB_REFUSAL + " I can draft the email if you tell me what it should say."),
    ]:
        check(f"refusal: {text}", agent.capability_refusal(text) == want, agent.capability_refusal(text))
    for text in ["Send an email to sam.lee@example.org", "Reply to Ravi", "Archive all promotions", "Move my 3pm call to 4pm",
                 "Delete the call with Andy Patel", "Forward the Atlassian email to Sam", "Trash the Google Play email"]:
        check(f"now allowed: {text}", agent.capability_refusal(text) is None, agent.capability_refusal(text))
    for t, want in [("yes", True), ("yes go ahead", True), ("ok send it", True), ("yes, but make it 4pm", False), ("yes add that contact", False)]:
        check(f"bare confirmation: {t}", agent.is_confirmation(t) == want)
    for t, want in [("Book Andy Patel for yesterday at 3pm", True), ("Add a call last Friday", True), ("What did Andy say yesterday?", False)]:
        check(f"past-date check: {t}", (agent.past_request_reply(t) is not None) == want)
    s3 = agent.new_session()
    ry = agent.run_turn(s3, "yes")
    check("bare yes with nothing pending", "no pending draft" in ry.reply, ry.reply)
    for text, want in [("Reply to Ravi saying thanks", "Ravi"), ("Forward Paula's email to Andy", "Paula"),
                       ("Forward the email from Cloudflare to Sam", "Cloudflare"), ("Reply to the Cloudflare email", "Cloudflare"),
                       ("Respond to Paula Sanders about the lawyer", "Paula Sanders")]:
        got = agent.followup_sender(text)
        check(f"follow-up name: {text}", got == want, got)
    for t, want in [("Reply to Ravi saying thanks, I will review it by Friday", ("reply", "Ravi", "thanks, I will review it by Friday", "")),
                    ("Forward Paula's email to sam.lee@example.org saying FYI", ("forward", "Paula", "FYI", "sam.lee@example.org")),
                    ("Forward the email from Cloudflare to Andy Chen", ("forward", "Cloudflare", "", "Andy Chen")),
                    ("What does Ravi want?", (None, "Ravi", "", ""))]:
        got = (agent.followup_kind(t), agent.followup_sender(t), agent._instruction(t), agent._forward_recipient(t) if agent.followup_kind(t) == "forward" else "")
        check(f"reply/forward parse: {t}", got == want, got)
    # ---- conversation context: "yes" and nameless replies, with a stand-in model (no Ollama)
    from types import SimpleNamespace
    prompts: list[str] = []

    def fake_complete(messages, tools_):
        prompts.append(messages[-1]["content"])
        text = "Hi Ravi,\nThanks, I will call you on Monday.\nAlex" if not tools_ else "Here are the details of both events."
        return SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=[]), prompt_eval_count=10, eval_count=5)

    real_complete = agent.llm.complete
    agent.llm.complete = fake_complete
    try:
        g.fail_send = False
        sq = agent.new_session()
        sq.messages += [{"role": "user", "content": f"{agent._now_tag()} what is on my calendar tomorrow?"},
                        {"role": "assistant", "content": "1. Call with Andy Chen\n2. Call Event\nWould you like more details on any of these events?"}]
        r = agent.run_turn(sq, "yes")
        check("yes after the assistant's question goes to the model", r.reply == "Here are the details of both events." and "no model call" not in r.footer, (r.reply, r.footer))
        sr = agent.new_session()
        focus = json.loads(tools.REGISTRY["read_email"].__wrapped__("m1")) if hasattr(tools.REGISTRY["read_email"], "__wrapped__") else None
        tok2 = obs.current_session.set(sr)
        try:
            focus_json = tools.read_email("m1")
        finally:
            obs.current_session.reset(tok2)
        sr.messages += [{"role": "user", "content": f"{agent._now_tag()} what does ravi want?"},
                        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_email", "arguments": {"message_id": "m1"}}}]},
                        {"role": "tool", "content": focus_json, "tool_name": "read_email"},
                        {"role": "assistant", "content": "Ravi wants the quote confirmed by Friday. Would you like to reply to Ravi now?"}]
        r = agent.run_turn(sr, "yes")
        pend = r.pending_draft or {}
        check("yes to an offer to reply prepares a reply card to that email", pend.get("mode") == "reply" and pend.get("source_message_id") == "m1"
              and pend.get("to") == ["reply+abc123@3.basecamp.com"], {k: pend.get(k) for k in ("mode", "source_message_id", "to")})
        agent.run_turn(sr, cancel_draft_id=pend.get("draft_id", ""))
        r = agent.run_turn(sr, "reply to him saying I will call him on Monday")
        pend = r.pending_draft or {}
        check("'reply to him saying ...' replies to the email in focus", pend.get("mode") == "reply" and pend.get("source_message_id") == "m1", pend.get("mode"))
        check("and passes the user's words to the writer", "I will call him on Monday" in prompts[-1], prompts[-1][-120:])
        r = agent.run_turn(sr, "yes")
        check("yes with a reply card pending points at the button", "click Send" in r.reply, r.reply)
        agent.run_turn(sr, cancel_draft_id=pend.get("draft_id", ""))
        r = agent.run_turn(sr, "forward it to sam.lee@example.org")
        pend = r.pending_draft or {}
        check("'forward it to <typed address>' forwards the email in focus", pend.get("mode") == "forward" and pend.get("to") == ["sam.lee@example.org"], {k: pend.get(k) for k in ("mode", "to")})
        check("nothing was sent by any of this", len(g.sent_raw) == 1, len(g.sent_raw))
        fresh = agent.run_turn(agent.new_session(), "yes go ahead")
        check("yes in a fresh chat still says nothing is pending", "no pending draft" in fresh.reply, fresh.reply)
    finally:
        agent.llm.complete = real_complete
    for t, want in [("reply to him saying I will call him on Monday", "reply"), ("yes, reply that I agree", "reply"), ("Forward it to Andy Chen", "forward"),
                    ("Reply to Ravi saying hi", None), ("Did Ravi reply?", None), ("Do I need to reply to anything?", None)]:
        check(f"contextual kind: {t}", agent.contextual_kind(t) == want, agent.contextual_kind(t))
    for frm, want in [('"Ravi Mehta (Basecamp)" <notifications@3.basecamp.com>', "Ravi"), ("Priya Shah <priya@acme.example>", "Priya"),
                      ("Cloudflare <em@em1.cloudflare.com>", ""), ("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", ""),
                      ("Google Play <googleplay-noreply@google.com>", "")]:
        check(f"greeting name: {frm}", agent._recipient_first_name(frm) == want, agent._recipient_first_name(frm))
    check("instruction from 'reply that ...'", agent._instruction("yes, reply that I agree with the quote") == "I agree with the quote", agent._instruction("yes, reply that I agree with the quote"))
    check("prose email is detected", agent.looks_like_prose_email("Subject: Hi\n\nHi Alex,\nThanks\nBest,\nAlex"))
    check("a normal reply is not a prose email", not agent.looks_like_prose_email("Ravi wants the quote by Friday."))
    for text in ["Brief my day", "What does my day look like?", "Give me my morning brief", "today's agenda"]:
        check(f"day brief intent: {text}", agent.is_day_brief_request(text))
    check("not a day brief: inbox", not agent.is_day_brief_request("Check my latest emails"))
    out = agent.format_day_brief(
        {"events": [{"summary": "Call with Andy Chen", "start": f"{tmr} 10:00", "end": f"{tmr} 10:30", "attendees": ["andy.chen@example.test"], "all_day": False}]},
        {"free": [{"from": "11:00", "to": "13:00"}]},
        {"unread_count": 2, "messages": [{"sender": "Ravi Mehta (Basecamp)", "subject": "Quote", "group": "needs_reply", "unread": True}]})
    check("day brief has agenda, free time and inbox", "10:00-10:30 Call with Andy Chen" in out and "11:00-13:00" in out and "**Needs a reply**" in out, out)

    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:400]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} manage checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
