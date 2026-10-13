"""Offline checks for the inbox brief, the classifier, follow-up detection and the offer scrub.
No model, no Google calls. Run: .venv/bin/python -m evals.test_inbox   (exit 1 on any failure)
"""
import sys

from assistant import agent, tools

CHECKS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


def group(sender, labels=(), **hdr):
    return tools._mail_group(sender, list(labels), {k.replace("_", "-"): v for k, v in hdr.items()}, "alex")


def main() -> int:
    # classifier, calibrated on the real inbox headers
    check("relayed person needs a reply", group('"Ravi Mehta (Basecamp)" <notifications@basecamp.com>', ["CATEGORY_PERSONAL"], precedence="bulk") == "needs_reply")
    check("own relayed post is an update", group('"Alex Kim (Basecamp)" <notifications@basecamp.com>', ["CATEGORY_PERSONAL"], precedence="bulk") == "updates")
    check("service digest is an update", group('"Basecamp (Acme Studio )" <notifications@basecamp.com>', ["CATEGORY_PERSONAL"]) == "updates")
    check("marketing with unsubscribe is a promotion", group("Cloudflare <em@em1.cloudflare.com>", ["CATEGORY_UPDATES"], list_unsubscribe="<mailto:x>") == "promotions")
    check("Gmail promotions label is a promotion", group("Atlassian <info@e.atlassian.com>", ["CATEGORY_PROMOTIONS"], list_unsubscribe="<x>") == "promotions")
    check("social notification is an update", group("Jordan Lee on LinkedIn <messages-noreply@linkedin.com>", ["CATEGORY_SOCIAL"], list_unsubscribe="<x>") == "updates")
    check("bounce is an update", group("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", ["CATEGORY_UPDATES"], auto_submitted="auto-replied") == "updates")
    check("plain mail from a person needs a reply", group("Priya Shah <priya@acme.example>", ["CATEGORY_PERSONAL"]) == "needs_reply")

    # formatter
    data = {"unread_count": 7, "unread_count_note": "exact", "more_available": False, "messages": [
        {"sender": "Ravi Mehta (Basecamp)", "subject": "Re: Design package - request for quote", "unread": True, "group": "needs_reply", "date": "2026-10-12 17:42"},
        {"sender": "Basecamp (Acme Studio )", "subject": "Basecamp (Acme Studio ): Here's the latest activity", "unread": True, "group": "updates"},
        {"sender": "Basecamp (Acme Studio )", "subject": "Basecamp (Acme Studio ): Here's the latest activity", "unread": False, "group": "updates"},
        {"sender": "Cloudflare", "subject": "Cloudflare Connect is almost sold out.", "unread": True, "group": "promotions"},
    ]}
    out = agent.format_inbox_brief(data)
    check("brief has the true unread count", out.startswith("Inbox, last 3 days: 7 unread."), out[:80])
    check("brief groups in order", out.index("**Needs a reply**") < out.index("**Updates**") < out.index("**Promotions**"), out)
    check("duplicates collapse with a count", "Here's the latest activity (x2, unread)" in out, out)
    check("sender prefix with colon is dropped", "Basecamp (Acme Studio) - Here's" in out, out)
    check("product names keep their first word", "Cloudflare - Cloudflare Connect is almost sold out." in out, out)
    check("brief suggests a follow-up", 'What does Ravi want?' in out, out[-80:])
    empty = agent.format_inbox_brief({"unread_count": 0, "messages": [{"sender": "Google Play", "subject": "Terms", "group": "updates"}]})
    check("says so when nothing is from a person", "Nothing from a person" in empty, empty)

    # brief intent
    brief = ["Check my latest emails and give me updates", "Check my latest email and give updates here.", "Any new emails?",
             "What's in my inbox?", "Summarise my inbox", "Show me unread emails", "inbox", "Which emails need a reply?",
             "Do I need to reply to anything?", "brief me on my email"]
    not_brief = ["What does Ravi want?", "Read the email from Ravi", "Any new emails from Andy?", "Did Andy send me an email?",
                 "What's the Cloudflare email about?", "Add a call with Andy tomorrow at 3pm", "What's on my calendar this week?"]
    for t in brief:
        check(f"brief intent: {t}", agent.is_inbox_brief_request(t))
    for t in not_brief:
        check(f"not brief: {t}", not agent.is_inbox_brief_request(t))

    # follow-up sender extraction
    for t, want in [("What does Ravi want?", "Ravi"), ("What did Paula Sanders say?", "Paula Sanders"),
                    ("What's the Cloudflare email about?", "Cloudflare"), ("Read the email from Paula", "Paula"),
                    ("What does it say?", None), ("Check my latest emails", None)]:
        got = agent.followup_sender(t)
        check(f"follow-up sender: {t}", got == want, got)

    # an email written out in the chat (instead of via draft_email) is detected; summaries are not
    check("prose email with a subject line", agent.looks_like_prose_email("Subject: Hello\n\nHi Alex,\n\nBest,\nAlex"))
    check("prose email with greeting and sign-off", agent.looks_like_prose_email("Hi Ravi,\nThe quote is approved.\nBest regards,\nAlex"))
    check("a summary is not a prose email", not agent.looks_like_prose_email("Ravi wants a quote for the design package by Friday."))

    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:300]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} inbox checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
