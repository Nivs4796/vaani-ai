"""Offline check of the capability guard: no model, no Google calls.

Sending, replying, forwarding, archiving and changing events now have tools (each behind
the Confirm card), so the guard only refuses what has no tool: web search and news,
permanent deletion, Gmail settings/filters/forwarding, and attachments. Read-only
questions about mail must never be blocked.

Run: .venv/bin/python -m evals.test_guard   (exit 1 on any miss)
"""
import sys

from assistant.agent import ATTACH_REFUSAL, PERMA_DELETE_REFUSAL, SETTINGS_REFUSAL, WEB_REFUSAL, capability_refusal

REFUSE = {
    "What's the latest news about Claude?": WEB_REFUSAL,
    "Search the web for Anthropic pricing": WEB_REFUSAL,
    "Look up the weather in Mumbai online": WEB_REFUSAL,
    "google the Claude release date": WEB_REFUSAL,
    'Can you send email to the "sam.lee@example.org" about the what lastest about the news about Claude ?': WEB_REFUSAL + " I can draft the email if you tell me what it should say.",
    "Permanently delete all Cloudflare emails": PERMA_DELETE_REFUSAL,
    "Delete the Basecamp digests forever": PERMA_DELETE_REFUSAL,
    "Empty the trash": PERMA_DELETE_REFUSAL,
    "Create a filter that archives LinkedIn mail": SETTINGS_REFUSAL,
    "Please set up forwarding to my work address": SETTINGS_REFUSAL,
    "Can you turn on my vacation responder?": SETTINGS_REFUSAL,
    "Change my Gmail signature": SETTINGS_REFUSAL,
    "Send Ravi the attached proposal": ATTACH_REFUSAL,
    "Forward the email with the attachment to Andy": ATTACH_REFUSAL,
}
ALLOW = [
    # now real actions, behind Confirm
    "Send an email to Andy Chen", "Reply to Ravi", "Please reply to the Cloudflare email", "Email Andy about the meeting",
    "Forward the Basecamp mail to Andy", "Draft an email to Priya", "Tell Ravi I will get back to him tomorrow",
    "Archive all promotions", "Mark the Google Play email as read", "Trash the Cloudflare emails", "Delete the Atlassian email",
    "Can you delete the call with Andy tomorrow?", "Move my 3pm call to 4pm", "Reschedule the meeting with Andy Patel",
    "Cancel my call with Andy Patel", "Accept the design review invite", "When am I free tomorrow?",
    # read-only questions
    "Check my latest emails and give me updates", "Did Andy send me an email?", "Who sent me the Basecamp email?",
    "What did Ravi reply?", "Do I need to reply to anything?", "Any news from Andy?", "What's on my calendar this week?",
    "Read the email from Ravi", "Search my inbox for mail from Andy", "Add a call with Andy tomorrow at 3pm",
    "Remember that Andy means Andy Chen", "Did Paula attach the contract?",
]


def main() -> int:
    misses = [("should refuse", t, capability_refusal(t)) for t, want in REFUSE.items() if capability_refusal(t) != want]
    misses += [("should allow", t, capability_refusal(t)) for t in ALLOW if capability_refusal(t)]
    for kind, t, got in misses:
        print(f"MISS  {kind}: {t}\n      got: {got}")
    total = len(REFUSE) + len(ALLOW)
    print(f"{total - len(misses)}/{total} guard checks passed")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
