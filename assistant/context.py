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
