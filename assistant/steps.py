"""Plain words for the live steps the page shows while a chat turn runs.

One row per @logged tool (assistant/tools.py, assistant/manage.py): what the step says
while it runs ("Reading your inbox") and once it is done ("Read your inbox"). A tool
that is not listed here shows as "Working" / "Worked", never as its code name.
"""

from __future__ import annotations

# tool name -> (while running, once done)
STEPS: dict[str, tuple[str, str]] = {
    "list_recent_emails": ("Reading your inbox", "Read your inbox"),
    "read_email": ("Opening the email", "Opened the email"),
    "list_calendar_events": ("Checking your calendar", "Checked your calendar"),
    "find_free_time": ("Looking for free time", "Looked for free time"),
    "find_contact": ("Looking up the contact", "Looked up the contact"),
    "draft_email": ("Writing the draft", "Wrote the draft"),
    "draft_nudge": ("Writing the nudge", "Wrote the nudge"),
    "draft_calendar_event": ("Drafting the event", "Drafted the event"),
    "draft_event_change": ("Preparing the calendar change", "Prepared the calendar change"),
    "draft_triage": ("Preparing the mailbox change", "Prepared the mailbox change"),
    "modify_emails": ("Preparing the mailbox change", "Prepared the mailbox change"),
    "commit_pending": ("Saving to Google", "Saved to Google"),
    "create_calendar_event": ("Saving to Google", "Saved to Google"),
    "save_contact": ("Saving the contact", "Saved the contact"),
    "remember": ("Saving to memory", "Saved to memory"),
}
UNKNOWN = ("Working", "Worked")


def label_for(tool: str) -> str:
    """What the step says while it runs, e.g. "Reading your inbox"."""
    return STEPS.get(str(tool or ""), UNKNOWN)[0]


def done_label_for(tool: str) -> str:
    """What the step says once it is done, e.g. "Read your inbox"."""
    return STEPS.get(str(tool or ""), UNKNOWN)[1]
