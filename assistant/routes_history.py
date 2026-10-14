"""Chat history routes: the rail's History list, reading an earlier chat, rename, delete, feedback.

Every route sits behind web.gate (401 without a user in google mode, X-CSRF-Token on writes).
A chat is keyed by (user, id), so another user's chat id answers 404 like an unknown one.
"live" means the chat session is still open in this server for this caller (web.SESSIONS).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from assistant import history

router = APIRouter()


class TitleIn(BaseModel):
    title: str


class FeedbackIn(BaseModel):
    session_id: str
    value: str
    excerpt: str | None = ""


def _live(request: Request) -> set[str]:
    from assistant import web

    uid = web._uid(request)
    return {sid for sid, e in list(web.SESSIONS.items()) if e.owner == uid}


def _unknown(chat_id: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": f"unknown chat {chat_id!r}"})


async def _run(request: Request, fn, *args):
    from assistant import web

    return await web._call_as(web._user(request), fn, *args)


@router.get("/chats")
async def chats(request: Request):
    from assistant import web

    return await _run(request, history.list_chats, web._uid(request), _live(request))


@router.get("/chats/{chat_id}")
async def chat(request: Request, chat_id: str):
    from assistant import web

    out = await _run(request, history.get_chat, web._uid(request), chat_id, _live(request))
    return out if out is not None else _unknown(chat_id)


@router.patch("/chats/{chat_id}")
async def rename(request: Request, chat_id: str, body: TitleIn):
    from assistant import web

    try:
        out = await _run(request, history.rename, web._uid(request), chat_id, body.title)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True, **out} if out is not None else _unknown(chat_id)


@router.delete("/chats/{chat_id}")
async def delete(request: Request, chat_id: str):
    from assistant import web

    ok = await _run(request, history.delete, web._uid(request), chat_id)
    return {"ok": True} if ok else _unknown(chat_id)


@router.post("/feedback")
async def feedback(request: Request, body: FeedbackIn):
    from assistant import web

    try:
        await _run(request, history.feedback, web._uid(request), body.session_id, body.value, body.excerpt or "")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True}
