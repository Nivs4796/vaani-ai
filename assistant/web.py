"""FastAPI front end: GET / (chat page), GET /me, POST /new, POST /chat.

Run:  .venv/bin/uvicorn assistant.web:app --port 8000

Sessions live in an in-process dict (see SESSIONS below). Each session has its
own lock so two requests for the same session never interleave their turns;
run_turn itself is synchronous and runs in FastAPI's threadpool so the event
loop stays free. Agent problems always come back as HTTP 200 with a friendly
reply; only a missing session or a malformed request is a 4xx.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from assistant import agent, google_auth, llm, memory

# uvicorn configures only its own loggers, so without this the "Ollama reachable,
# model present" line never reaches the console.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore"):  # one line per Ollama request otherwise
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("assistant.web")
log.setLevel(logging.INFO)

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "static" / "index.html"
SCOPES = [s.rsplit("/", 1)[-1] for s in google_auth.SCOPES]


@dataclass
class _Entry:
    session: agent.Session
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# Session store. Postgres swap point: replace this dict with a table keyed by
# session id holding the JSON transcript + drafts (agent.Session is plain data),
# load on request and save after each turn. Nothing else in this file changes.
# ---------------------------------------------------------------------------
SESSIONS: dict[str, _Entry] = {}

STATE: dict = {"email": None, "email_error": None, "model_present": None}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(memory.load().get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _connected_email() -> str:
    if STATE["email"]:
        return STATE["email"]
    try:
        STATE["email"] = google_auth.connected_email()
        STATE["email_error"] = None
    except Exception as e:  # keep serving; the header will say so
        STATE["email_error"] = f"{type(e).__name__}: {e}"
        log.error("Google not connected: %s", STATE["email_error"])
        return f"(Google not connected: {type(e).__name__})"
    return STATE["email"]


def _check_model() -> bool | None:
    """Ping Ollama now (5 s timeout) and refresh STATE['model_present']."""
    try:
        info = llm.check_server()
        STATE["model_present"] = info["model_present"]
        if info["model_present"]:
            log.info("Ollama %s reachable, model %s present", info["version"], info["model"])
        else:
            log.error("!!! MODEL %s IS NOT IN `ollama list` (Ollama %s is up). Run: ollama pull %s !!!", info["model"], info["version"], info["model"])
    except llm.LLMError as e:
        STATE["model_present"] = False
        log.error("!!! OLLAMA CHECK FAILED: %s !!!", e)
    return STATE["model_present"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(_check_model)
    await run_in_threadpool(_connected_email)
    yield


app = FastAPI(title="Vaani AI", lifespan=lifespan)


class ChatIn(BaseModel):
    session_id: str
    text: str | None = None
    confirm_draft_id: str | None = None
    cancel_draft_id: str | None = None
    confirm_variant: str | None = None


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/me")
async def me():
    tz = _tz()
    email = await run_in_threadpool(_connected_email)
    await run_in_threadpool(_check_model)  # live, not the value cached at startup
    return {
        "email": email,
        "scopes": SCOPES,
        "now": datetime.now(tz).strftime("%a %d %b %H:%M"),
        "tz": tz.key,
        "model": llm.MODEL,
        "model_present": STATE["model_present"],
    }


@app.post("/new")
async def new():
    session = agent.new_session()
    SESSIONS[session.id] = _Entry(session=session)
    return {"session_id": session.id}


def _friendly(session: agent.Session, text: str) -> dict:
    r = agent.TurnResult(
        reply=text,
        pending_draft=agent.pending_draft(session),
        tool_calls=[],
        usage={"prompt_tokens": 0, "gen_tokens": 0, "model_calls": 0},
        latency_ms=0,
        model=llm.MODEL,
        footer=agent.footer_for(0, 0, 0, 0),
    )
    return asdict(r)


@app.post("/chat")
async def chat(body: ChatIn):
    entry = SESSIONS.get(body.session_id)
    if entry is None:
        return JSONResponse(status_code=404, content={"error": f"unknown session_id {body.session_id!r}; POST /new to start one"})
    async with entry.lock:  # one turn at a time per session
        try:
            result = await run_in_threadpool(
                agent.run_turn,
                entry.session,
                body.text,
                confirm_draft_id=body.confirm_draft_id,
                cancel_draft_id=body.cancel_draft_id,
                confirm_variant=body.confirm_variant,
            )
            return asdict(result)
        except Exception as e:  # run_turn already catches; this is the backstop, never a 500
            log.exception("run_turn crashed")
            return _friendly(entry.session, f"Something went wrong on my side ({type(e).__name__}). Nothing was changed; please try again.")


@app.exception_handler(404)
async def not_found(request, exc):
    detail = getattr(exc, "detail", "not found")
    return JSONResponse(status_code=404, content={"error": detail})


@app.exception_handler(RequestValidationError)
async def bad_request(request, exc: RequestValidationError):
    # Same {"error": ...} shape as every other error so the page can show it.
    problems = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}" for e in exc.errors())
    return JSONResponse(status_code=422, content={"error": f"bad request: {problems or 'invalid body'}"})
