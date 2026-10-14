"""Who the current request is for.

web.py sets current_user inside the worker thread that runs a turn (or a route),
so memory, google_auth and obs can find the signed-in user without every
function growing a user parameter. None means single-user mode (VAANI_AUTH=single,
the REPL, scripts and tests): everything falls back to today's files.
"""

from __future__ import annotations

from contextvars import ContextVar

# {"id": "<uuid>", "email": "...", "name": "...", "picture": "..."} or None
current_user: ContextVar[dict | None] = ContextVar("current_user", default=None)


def user_id() -> str | None:
    u = current_user.get()
    return (u or {}).get("id") or None


def demo_label() -> str:
    """VAANI_DEMO_LABEL: a Gmail label that fences the app in for a recorded demo (empty = off)."""
    import os

    return os.environ.get("VAANI_DEMO_LABEL", "").strip()


def gmail_scope(query: str) -> str:
    """Every Gmail search Vaani runs goes through here. With VAANI_DEMO_LABEL set, only mail
    carrying that label is ever listed, so a screen recording never shows the real inbox."""
    label = demo_label()
    q = " ".join(str(query or "").split())
    if not label:
        return q
    tag = f"label:{label}"
    return q if tag in q else f"({q}) {tag}" if q else tag
