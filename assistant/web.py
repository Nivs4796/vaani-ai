"""FastAPI front end: GET / (chat page), GET /me, POST /new, POST /chat, Google sign-in.

Run:  .venv/bin/uvicorn assistant.web:app --port 8000

Sessions live in an in-process dict (see SESSIONS below). Each session has its
own lock so two requests for the same session never interleave their turns;
run_turn itself is synchronous and runs in FastAPI's threadpool so the event
loop stays free. Agent problems always come back as HTTP 200 with a friendly
reply; only a missing session or a malformed request is a 4xx.

VAANI_AUTH=single (default): no sign-in, everything uses token.json and
data/memory.json exactly as before. VAANI_AUTH=google: GET / sends visitors to
/login, "Sign in with Google" creates one Vaani account per Google user
(auth.py), the signed session cookie "vaani_session" carries the user and a
CSRF token, every other route needs that session (401) and every POST/PUT needs
the X-CSRF-Token header from GET /me (403). Each request's work runs with
context.current_user set inside the worker thread (_call_as), so memory and
google_auth pick that user's files and token.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from assistant import agent, auth, context, flow, google_auth, llm, memory, store

# uvicorn configures only its own loggers, so without this the "Ollama reachable,
# model present" line never reaches the console.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore"):  # one line per Ollama request otherwise
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("assistant.web")
log.setLevel(logging.INFO)

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"
LOGIN_HTML = STATIC_DIR / "login.html"
SCOPES = [s.rsplit("/", 1)[-1] for s in google_auth.SCOPES]

SESSION_COOKIE = "vaani_session"
SESSION_MAX_AGE = 7 * 24 * 3600
OAUTH_PENDING_MAX_S = 600  # a consent screen left open longer than this must start again
# Reachable without a signed-in user in google mode; everything else answers 401.
PUBLIC_PATHS = {"/", "/login", "/auth/login", "/auth/callback", "/auth/logout", "/me", "/favicon.ico"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class _Entry:
    session: agent.Session
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: str | None = None  # user id in google mode; None in single mode


# ---------------------------------------------------------------------------
# Session store. Postgres swap point: replace this dict with a table keyed by
# session id holding the JSON transcript + drafts (agent.Session is plain data),
# load on request and save after each turn. Nothing else in this file changes.
# ---------------------------------------------------------------------------
SESSIONS: dict[str, _Entry] = {}

STATE: dict = {"email": None, "email_error": None, "model_present": None}


def _tz(user_id: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(memory.load(user_id).get("timezone") or "UTC")
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


def _reauth_required(user_id: str) -> bool:
    """Builds (once) and checks this user's Google clients; True when they must sign in again."""
    try:
        google_auth.connected_email(user_id)
        return False
    except google_auth.ReauthRequired:
        return True
    except Exception as e:  # network trouble is not a reason to sign out
        log.error("Google check failed for user %s: %s: %s", user_id, type(e).__name__, e)
        return google_auth.needs_reauth(user_id)


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


def _flag(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def _flags() -> dict:
    theme = os.environ.get("VAANI_THEME", "classic").strip().lower()
    return {
        "autonomy": _flag("VAANI_AUTONOMY"),
        "promotion": _flag("VAANI_PROMOTION"),
        "brief_on_open": _flag("VAANI_BRIEF_ON_OPEN"),
        "followups": _flag("VAANI_FOLLOWUPS"),
        "theme": theme if theme in ("classic", "ledger") else "classic",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(_check_model)
    if auth.mode() == "google":
        log.info("Sign-in: Google (VAANI_AUTH=google); open http://127.0.0.1:<port>/ to sign in")
        if not os.environ.get("VAANI_TOKEN_KEY"):
            log.error("!!! VAANI_TOKEN_KEY is not set: sign-in cannot store Google tokens. Run: python -m assistant.auth init-env !!!")
    else:
        await run_in_threadpool(_connected_email)
    try:  # speech-to-text loads in a background thread; /voice/status reports when it is ready
        from assistant import voice

        voice.start_warmup()
    except Exception:
        log.exception("voice warm-up did not start")
    yield


app = FastAPI(title="Vaani AI", lifespan=lifespan)


# --------------------------------------------------------------------------- users and sessions


def _user(request: Request) -> dict | None:
    """The signed-in user ({id, email, name, picture}) or None (single mode, or not signed in)."""
    return getattr(request.state, "user", None)


def _uid(request: Request) -> str | None:
    return (_user(request) or {}).get("id") or None


async def _call_as(user: dict | None, fn, *args, **kwargs):
    """Run fn in the threadpool with context.current_user = user, set INSIDE the worker thread,
    so memory, google_auth and obs see this request's user and nobody else's."""

    def run():
        token = context.current_user.set(user)
        try:
            return fn(*args, **kwargs)
        finally:
            context.current_user.reset(token)

    return await run_in_threadpool(run)


def _entry_for(request: Request, session_id: str) -> _Entry | None:
    """The chat session, only if it belongs to this request's user (another user's id looks unknown)."""
    entry = SESSIONS.get(session_id)
    if entry is None or entry.owner != _uid(request):
        return None
    return entry


def _unknown_session(session_id: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": f"unknown session_id {session_id!r}; POST /new to start one"})


def _drop_sessions(user_id: str) -> None:
    for sid in [sid for sid, e in SESSIONS.items() if e.owner == user_id]:
        SESSIONS.pop(sid, None)


def _csrf_ok(request: Request) -> bool:
    want = request.session.get("csrf")
    got = request.headers.get("x-csrf-token", "")
    return bool(want) and hmac.compare_digest(str(want), str(got))


@app.middleware("http")
async def gate(request: Request, call_next):
    """google mode: put the session's user on request.state, then 401 without one and 403 on a
    POST/PUT without the session's X-CSRF-Token. single mode: pass everything through."""
    request.state.user = None
    if auth.mode() != "google":
        return await call_next(request)
    user = request.session.get("user")
    if isinstance(user, dict) and user.get("id"):
        request.state.user = user
    path = request.url.path
    public = path in PUBLIC_PATHS or path == "/static" or path.startswith("/static/")
    if request.state.user is None and not public:
        return JSONResponse(status_code=401, content={"error": "sign in required"})
    if request.state.user is not None and request.method in UNSAFE_METHODS and not _csrf_ok(request):
        return JSONResponse(status_code=403, content={"error": "missing or stale X-CSRF-Token header; reload the page"})
    return await call_next(request)


# Added after gate so it wraps it: the session is decoded before gate reads it.
_session_secret = os.environ.get("VAANI_SESSION_SECRET", "").strip()
if not _session_secret:
    if auth.mode() == "google":
        log.warning("VAANI_SESSION_SECRET is not set; sign-ins will not survive a server restart (run: python -m assistant.auth init-env)")
    _session_secret = secrets.token_urlsafe(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie=SESSION_COOKIE,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,  # loopback http; the cookie is still HttpOnly and signed
)
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


class ChatIn(BaseModel):
    session_id: str
    text: str | None = None
    confirm_draft_id: str | None = None
    cancel_draft_id: str | None = None
    confirm_variant: str | None = None
    via: str | None = None  # "typed" | "voice" | "chip": how the text was entered (history, voice rules)


# --------------------------------------------------------------------------- sign-in


def _login_error(message: str) -> RedirectResponse:
    return RedirectResponse("/login?error=" + quote(message), status_code=302)


_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vaani AI · Sign in</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e2e2dc;--err:#a3271c}}
@media (prefers-color-scheme: dark){{:root{{--bg:#161615;--fg:#ededea;--muted:#a0a09a;--card:#20201e;--line:#34342f;--err:#f08a7e}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:0 16px}}
main{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:32px;max-width:380px;width:100%;box-sizing:border-box}}
h1{{margin:0 0 4px;font-size:22px}} p{{color:var(--muted);margin:0 0 20px}} .err{{color:var(--err)}}
a.btn{{display:block;text-align:center;padding:10px 14px;border-radius:8px;border:1px solid var(--line);color:var(--fg);text-decoration:none;font-weight:600}}
a.btn:hover{{border-color:var(--muted)}}
</style></head><body><main>
<h1>Vaani AI</h1><p>Your Gmail and Calendar assistant on a local model.</p>
{error}<a class="btn" href="/auth/login">Sign in with Google</a>
</main></body></html>
"""


@app.get("/login")
async def login_page(request: Request):
    if auth.mode() != "google" or _user(request):
        return RedirectResponse("/", status_code=302)
    if LOGIN_HTML.exists():
        return FileResponse(LOGIN_HTML, media_type="text/html")
    error = request.query_params.get("error", "")
    block = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(_LOGIN_PAGE.format(error=block))


@app.get("/auth/login")
async def auth_login(request: Request):
    if auth.mode() != "google":
        return RedirectResponse("/", status_code=302)
    try:
        redirect_uri = auth.redirect_uri_for(request.url.scheme, request.url.netloc)
        started = await run_in_threadpool(auth.start, redirect_uri)
    except auth.AuthError as e:
        return _login_error(str(e))
    except Exception as e:  # missing credentials.json and the like: say so, never a 500
        log.exception("could not start Google sign-in")
        return _login_error(f"could not start Google sign-in ({type(e).__name__}: {e})")
    request.session["oauth"] = {
        "state": started["state"],
        "verifier": started["code_verifier"],
        "redirect_uri": redirect_uri,
        "at": int(time.time()),
    }
    return RedirectResponse(started["url"], status_code=302)


async def _finish_signin(request: Request):
    q = request.query_params
    pending = request.session.pop("oauth", None)  # single use, whatever happens next
    if q.get("error"):
        return _login_error(f"Google sign-in did not complete ({q.get('error')})")
    fresh = isinstance(pending, dict) and time.time() - float(pending.get("at") or 0) < OAUTH_PENDING_MAX_S
    if not fresh or not hmac.compare_digest(str(q.get("state", "")), str(pending.get("state", ""))):
        return _login_error("that sign-in has expired or was started in another browser; please sign in again")
    try:
        user = await run_in_threadpool(auth.finish, q.get("code", ""), pending["redirect_uri"], pending["state"], pending["verifier"])
    except (auth.AuthError, store.StoreError) as e:
        log.warning("sign-in refused: %s", e)
        return _login_error(str(e))
    except Exception as e:
        log.exception("sign-in crashed")
        return _login_error(f"sign-in failed on our side ({type(e).__name__}); please try again")
    request.session.clear()  # a fresh session per sign-in (no fixation)
    request.session["user"] = user
    request.session["csrf"] = secrets.token_urlsafe(32)
    log.info("signed in %s (user %s)", user["email"], user["id"])
    return RedirectResponse("/", status_code=302)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if auth.mode() != "google":
        return RedirectResponse("/", status_code=302)
    return await _finish_signin(request)


@app.post("/auth/logout")
async def auth_logout(request: Request):
    uid = _uid(request)
    if auth.mode() == "google":
        request.session.clear()
    if uid:
        _drop_sessions(uid)
    return {"ok": True}


@app.post("/auth/disconnect")
async def auth_disconnect(request: Request):
    """Revoke Vaani's Google access for this user and forget the token (the account and memory stay)."""
    if auth.mode() != "google":
        return JSONResponse(status_code=400, content={"error": "sign-in is off (VAANI_AUTH=single); nothing to disconnect"})
    uid = _uid(request)

    def work() -> bool:
        try:
            tok = store.get_token(uid)
        except store.StoreError:
            tok = None
        revoked = auth.revoke(tok["refresh_token"]) if tok else False
        store.delete_token(uid)
        google_auth.forget(uid)
        return revoked

    try:
        revoked = await run_in_threadpool(work)
    except Exception as e:
        log.exception("disconnect failed")
        return JSONResponse(status_code=502, content={"error": f"could not disconnect Google ({type(e).__name__}); nothing was changed"})
    request.session.clear()
    _drop_sessions(uid)
    return {"ok": True, "revoked": revoked}


# --------------------------------------------------------------------------- app routes


@app.get("/")
async def index(request: Request):
    if auth.mode() == "google":
        q = request.query_params
        if q.get("state") and (q.get("code") or q.get("error")):  # Google's redirect lands on the root
            return await _finish_signin(request)
        if _user(request) is None:
            return RedirectResponse("/login", status_code=302)
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/me")
async def me(request: Request):
    google = auth.mode() == "google"
    user = _user(request)
    uid = _uid(request)
    tz = _tz(uid)
    if google:
        email = user["email"] if user else None
        reauth = await run_in_threadpool(_reauth_required, uid) if uid else False
    else:
        email = await run_in_threadpool(_connected_email)
        reauth = False
    await run_in_threadpool(_check_model)  # live, not the value cached at startup
    return {
        "email": email,
        "scopes": SCOPES,
        "now": datetime.now(tz).strftime("%a %d %b %H:%M"),
        "tz": tz.key,
        "model": llm.MODEL,
        "model_present": STATE["model_present"],
        "auth_mode": auth.mode(),
        "user": auth.public(user),
        "csrf": request.session.get("csrf") if (google and user) else None,
        "reauth_required": reauth,
        "flags": _flags(),
    }


@app.post("/new")
async def new(request: Request):
    user = _user(request)
    session = await _call_as(user, agent.new_session)
    while session.id in SESSIONS:  # 8 hex chars: never hand one user's id to another
        session = await _call_as(user, agent.new_session)
    SESSIONS[session.id] = _Entry(session=session, owner=_uid(request))
    brief = None
    if flow.brief_on_open():
        try:
            brief = await _call_as(user, flow.open_chat, session)
        except Exception:  # the chat still opens; the page shows the old placeholder
            log.exception("brief on open failed")
    out = {"session_id": session.id, "brief": brief}
    await _remember(request, session.id, "new", {}, out)
    return out


async def _remember(request: Request, session_id: str, kind: str, req: dict, res) -> None:
    """Store one turn in the chat history (assistant/history.py). Never fails the request."""
    if not isinstance(res, dict):
        return
    try:
        from assistant import history

        await run_in_threadpool(history.record, _uid(request), session_id, kind, req, res)
    except Exception:
        log.exception("history.record failed")


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
async def chat(request: Request, body: ChatIn):
    entry = _entry_for(request, body.session_id)
    if entry is None:
        return _unknown_session(body.session_id)
    async with entry.lock:  # one turn at a time per session
        try:
            user = _user(request)
            if body.cancel_draft_id is not None:  # Cancel and Skip are the same decision: logged as a skip
                out = await _call_as(user, flow.skip, entry.session, body.cancel_draft_id)
                await _remember(request, body.session_id, "cancel", {"draft_id": body.cancel_draft_id}, out)
                return out
            if body.confirm_draft_id is not None:
                out = await _call_as(user, flow.confirm, entry.session, body.confirm_draft_id, body.confirm_variant)
                await _remember(request, body.session_id, "confirm", {"draft_id": body.confirm_draft_id, "variant": body.confirm_variant}, out)
                return out
            out = await _call_as(user, flow.chat, entry.session, body.text)
            await _remember(request, body.session_id, "chat", {"text": body.text, "via": body.via or "typed"}, out)
            return out
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


# --- integration routes (coordinator) ---
# Sign-in (401) and X-CSRF-Token (403) are already enforced by gate() for every route added
# here. Use: user = _user(request) (None in single mode); entry = _entry_for(request, session_id)
# (owner-checked, None -> return _unknown_session(session_id)); and run anything that reads
# memory, Google or obs through `await _call_as(user, fn, *args, **kwargs)`.


class ActionIn(BaseModel):
    session_id: str
    action: str
    message_id: str | None = None
    thread_id: str | None = None
    draft_id: str | None = None  # undo: the carried-out card to reverse


class SkipIn(BaseModel):
    session_id: str
    draft_id: str


class LevelIn(BaseModel):
    level: str | None = None
    send_unlocked: bool | None = None


class OfferIn(BaseModel):
    action: str


async def _in_session(request: Request, session_id: str, fn, *args):
    """Run a flow function for one chat session: owner-checked, one turn at a time, never a 500."""
    entry = _entry_for(request, session_id)
    if entry is None:
        return _unknown_session(session_id)
    async with entry.lock:
        try:
            return await _call_as(_user(request), fn, entry.session, *args)
        except Exception as e:
            log.exception("%s crashed", getattr(fn, "__name__", "flow call"))
            return _friendly(entry.session, f"Something went wrong on my side ({type(e).__name__}). Nothing was changed; please try again.")


@app.post("/action")
async def brief_action(request: Request, body: ActionIn):
    out = await _in_session(request, body.session_id, flow.action, body.action, body.message_id, body.thread_id, body.draft_id)
    await _remember(request, body.session_id, "action", {"action": body.action, "message_id": body.message_id,
                                                          "thread_id": body.thread_id, "draft_id": body.draft_id}, out)
    return out


@app.post("/skip")
async def skip(request: Request, body: SkipIn):
    return await _in_session(request, body.session_id, flow.skip, body.draft_id)


@app.get("/brief")
async def get_brief(request: Request, session_id: str | None = None, refresh: int = 0):
    entry = _entry_for(request, session_id) if session_id else None
    session = entry.session if entry else None
    if refresh or session is None:
        return await _call_as(_user(request), flow.refresh_brief, session)
    return flow.public_brief(getattr(session, "brief", None)) or await _call_as(_user(request), flow.refresh_brief, session)


def _autonomy_rows() -> dict:
    from assistant import autonomy

    state = autonomy.get_state()
    rows = []
    for wf in autonomy.WORKFLOWS:
        row = dict(state.get(wf) or {})
        row["key"] = wf
        row.setdefault("title", getattr(autonomy, "TITLES", {}).get(wf, wf.replace("_", " ").title()))
        rows.append(row)
    return {"workflows": rows}


@app.get("/autonomy")
async def get_autonomy(request: Request):
    return await _call_as(_user(request), _autonomy_rows)


def _set_level(workflow: str, level: str | None, send_unlocked: bool | None) -> dict:
    from assistant import autonomy

    if workflow not in autonomy.WORKFLOWS:
        raise ValueError(f"unknown workflow {workflow!r}")
    current = (autonomy.get_state().get(workflow) or {}).get("level")
    autonomy.set_level(workflow, level or current, send_unlocked=send_unlocked)
    return next(r for r in _autonomy_rows()["workflows"] if r["key"] == workflow)


@app.put("/autonomy/{workflow}")
async def put_autonomy(request: Request, workflow: str, body: LevelIn):
    try:
        return await _call_as(_user(request), _set_level, workflow, body.level, body.send_unlocked)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


def _offer_action(workflow: str, action: str) -> dict:
    from assistant import autonomy

    if action == "accept":
        autonomy.accept_offer(workflow)
    elif action == "snooze":
        autonomy.snooze_offer(workflow)
    elif action == "step_back":
        autonomy.accept_stepback(workflow)
    elif action == "keep":
        autonomy.keep_level(workflow)
    else:
        raise ValueError(f"action must be accept, snooze, step_back or keep, not {action!r}")
    return next(r for r in _autonomy_rows()["workflows"] if r["key"] == workflow)


@app.post("/promotion/{workflow}")
async def promotion(request: Request, workflow: str, body: OfferIn):
    try:
        return await _call_as(_user(request), _offer_action, workflow, body.action)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


def _followup_rows() -> dict:
    from assistant import followups

    return {"rows": followups.candidates()}


@app.get("/followups")
async def get_followups(request: Request):
    return await _call_as(_user(request), _followup_rows)


@app.post("/followups/{thread_id}/dismiss")
async def dismiss_followup(request: Request, thread_id: str):
    from assistant import followups

    await _call_as(_user(request), followups.dismiss, thread_id)
    return {"ok": True}


@app.get("/proof")
async def get_proof(request: Request, workflow: str | None = None, limit: int = 20):
    return await _call_as(_user(request), flow.proof, workflow, max(1, min(int(limit), 100)))



# --- look, feel and voice build: one router per feature module, each owned by one agent ---
# (.build/CONTRACT-v2.md). Imported last so the routers can lazily reuse this module's helpers.
from assistant import routes_history, routes_rail, routes_stream, routes_voice, routes_voiceturn  # noqa: E402

# voice-first HUD build (.build/CONTRACT-v3.md): routes_voiceturn adds /voice/mode, /voice/turn, /voice/commit ...
for _router in (routes_stream.router, routes_history.router, routes_rail.router, routes_voice.router,
                routes_voiceturn.router):
    app.include_router(_router)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Pages and static files are revalidated on every load (cheap 304s), so a browser never
    runs a new page against a cached old app.js or app.css after an update."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path in ("/", "/login"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response
