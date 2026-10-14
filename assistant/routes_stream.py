"""POST /chat/stream: one chat turn with live steps, as Server-Sent Events.

While the turn runs, every @logged tool call is reported as a step (obs.step_sink), so
the page can say "Reading your inbox…" as it happens. The reply itself is sent once,
inside the "result" event, after every guard in agent.run_turn has run: model tokens
are never streamed, because unguarded text must never reach the page.

Events, one JSON object per `data:` line (contract: .build/CONTRACT-v2.md):
  {"type": "step", "label", "tool", "phase": "start"|"end", "ms", "ok"}   (zero or more;
      an "end" step also carries "done", the past-tense label: "Read your inbox")
  {"type": "result", "result": <exactly the dict POST /chat returns>}
  {"type": "done"}
{"type": "error", "error": "..."} replaces "result" only if not even the backstop reply
could be built. An unknown session or a bad body is a plain JSON 404/422, like /chat.

Everything else mirrors web.chat(): owner check, one turn at a time per session (the
session lock), the same backstop reply, the same history row. The turn runs as its own
task, so a closed tab never cuts a turn short or leaves the lock half-held.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging

from fastapi import APIRouter, Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from assistant import obs, steps

router = APIRouter()
log = logging.getLogger("assistant.routes_stream")

_END = object()  # queue sentinel: the turn task has said everything it will say
_RUNNING: set[asyncio.Task] = set()  # strong refs, so a running turn is never garbage-collected


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"


def _step(ev: dict) -> dict:
    tool = str(ev.get("tool") or "")
    out = {"type": "step", "label": steps.label_for(tool), "tool": tool, "phase": ev.get("phase"),
           "ms": ev.get("ms"), "ok": ev.get("ok")}
    if ev.get("phase") == "end":
        out["done"] = steps.done_label_for(tool)
    return out


@router.post("/chat/stream")
async def chat_stream(request: Request, raw: dict = Body(...)):
    from assistant import flow, web  # lazy: web imports this module

    try:  # the body is web.ChatIn; validated here because web cannot be imported at load time
        body = web.ChatIn.model_validate(raw)
    except ValidationError as e:
        raise RequestValidationError(e.errors()) from None
    entry = web._entry_for(request, body.session_id)
    if entry is None:
        return web._unknown_session(body.session_id)
    if body.cancel_draft_id is not None:  # Cancel and Skip are the same decision: logged as a skip
        kind, req = "cancel", {"draft_id": body.cancel_draft_id}
        call = functools.partial(flow.skip, entry.session, body.cancel_draft_id)
    elif body.confirm_draft_id is not None:
        kind, req = "confirm", {"draft_id": body.confirm_draft_id, "variant": body.confirm_variant}
        call = functools.partial(flow.confirm, entry.session, body.confirm_draft_id, body.confirm_variant)
    else:
        kind, req = "chat", {"text": body.text, "via": body.via or "typed"}
        call = functools.partial(flow.chat, entry.session, body.text)

    return sse_turn(request, entry, body.session_id, kind, req, call)


def sse_turn(request: Request, entry, session_id: str, kind: str, req: dict, call, setup=None) -> StreamingResponse:
    """Run call() (a flow function returning the /chat dict) as one streamed turn: live steps,
    then {"type": "result"}, then {"type": "done"}. Holds the session lock, records history as
    `kind` with `req`, and never 500s. setup(), if given, runs inside the worker thread first and
    may return a callable run afterwards (context vars for voice mode). Shared with routes_voiceturn.py."""
    from assistant import web  # lazy: web imports this module

    user = web._user(request)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def push(ev: dict) -> None:  # called in the worker thread by obs.logged
        loop.call_soon_threadsafe(queue.put_nowait, _step(ev))

    def work():
        token = obs.step_sink.set(push)
        undo = setup() if setup else None
        try:
            return call()
        finally:
            if callable(undo):
                undo()
            obs.step_sink.reset(token)

    async def run() -> None:
        try:
            async with entry.lock:  # one turn at a time per session
                try:
                    out = await web._call_as(user, work)
                    await web._remember(request, session_id, kind, req, out)
                except Exception as e:  # run_turn already catches; this is the backstop, never a 500
                    log.exception("run_turn crashed")
                    out = web._friendly(entry.session, f"Something went wrong on my side ({type(e).__name__}). Nothing was changed; please try again.")
            queue.put_nowait({"type": "result", "result": out})
        except Exception as e:
            log.exception("chat stream failed")
            queue.put_nowait({"type": "error", "error": f"the turn failed on our side ({type(e).__name__})"})
        finally:
            queue.put_nowait(_END)

    task = asyncio.create_task(run())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)

    async def events():
        while True:
            ev = await queue.get()
            if ev is _END:
                break
            yield _sse(ev)
        yield _sse({"type": "done"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
