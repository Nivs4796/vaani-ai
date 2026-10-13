"""Offline checks for Google sign-in, Vaani accounts and per-user memory.

No network, no Google, no model: the three Google calls in auth.py (_exchange_code,
_verify_id_token, revoke) are replaced by fakes, credentials.json by a fake Desktop
client, and data/memory.json, data/users/ and data/vaani.db by a temp directory.
Proves: tokens are encrypted at rest, an imported account is claimed by its first real
sign-in, two users never see each other's memory or chat sessions, single mode needs no
cookie, and google mode refuses a forged callback, a missing CSRF header and a stranger.

Run: .venv/bin/python -m evals.test_auth   (exit 1 on any failure)
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.fernet import Fernet

os.environ["VAANI_TOKEN_KEY"] = Fernet.generate_key().decode()
os.environ["VAANI_ALLOWED_EMAILS"] = ""  # empty, not unset: python-dotenv would fill an unset one from .env
warnings.filterwarnings("ignore", message=".*httpx.*")

from fastapi.testclient import TestClient  # noqa: E402
from google.auth.exceptions import RefreshError  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from requests_oauthlib import OAuth2Session  # noqa: E402

from assistant import agent, auth, cli, context, google_auth, llm, memory, store, web  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
OWN = "me@example.test"
CLIENT_ID = "test-client.apps.googleusercontent.com"
BASE = "http://127.0.0.1:8031"


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


# --------------------------------------------------------------------------- fakes

IDENTITIES = {
    "codeA": {"sub": "sub-A", "email": "Boss@Acme.example", "email_verified": True, "name": "Alex Boss", "given_name": "Alex", "picture": "https://pic/a"},
    "codeB": {"sub": "sub-B", "email": "priya@acme.example", "email_verified": True, "name": "Priya Shah", "given_name": "Priya", "picture": "https://pic/b"},
    "codeC": {"sub": "sub-C", "email": "stranger@else.example", "email_verified": True, "name": "Stranger", "given_name": "Stranger"},
    "codeU": {"sub": "sub-U", "email": "unverified@acme.example", "email_verified": False, "name": "Unverified"},
    "codeS": {"sub": "sub-S", "email": "scopes@acme.example", "email_verified": True, "name": "Few Scopes"},
}
EXCHANGES: list[dict] = []
REVOKED: list[str] = []


def fake_exchange(code, redirect_uri, state, code_verifier):
    EXCHANGES.append({"code": code, "redirect_uri": redirect_uri, "state": state, "verifier": code_verifier})
    if code not in IDENTITIES:
        raise ValueError("invalid_grant")
    scopes = list(auth.SIGNIN_SCOPES)
    if code == "codeS":
        scopes = [s for s in scopes if "gmail" not in s]
    return {"refresh_token": f"rt-{code}", "id_token": f"idt-{code}", "scopes": scopes}


def fake_verify(raw, client_id):
    if client_id != CLIENT_ID:
        raise ValueError("wrong audience")
    return dict(IDENTITIES[raw.removeprefix("idt-")])


def fake_revoke(token):
    REVOKED.append(token)
    return True


@dataclass
class FakeTurn:
    reply: str
    who: str
    pending_draft: dict | None = None  # web.py runs turns through flow, which reads this


def fake_run_turn(session, text=None, **kw):
    # Runs inside the worker thread: proves _call_as set the user there.
    return FakeTurn(reply=str(context.user_id()), who=str(memory.load().get("name")))


def setup() -> Path:
    tmp = Path(tempfile.mkdtemp())
    memory.MEMORY_FILE = tmp / "memory.json"
    memory.USERS_DIR = tmp / "users"
    store.DB_FILE = tmp / "vaani.db"
    single = copy.deepcopy(memory.DEFAULT)
    single.update(name="Alex", timezone="Asia/Kolkata", contacts=[{"name": "Ravi Mehta", "email": "ravi@acme.example"}], facts=["single fact"])
    memory.MEMORY_FILE.write_text(json.dumps(single))
    creds = tmp / "credentials.json"
    creds.write_text(json.dumps({"installed": {
        "client_id": CLIENT_ID, "client_secret": "test-secret", "project_id": "test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"]}}))
    google_auth.CREDENTIALS_FILE = creds
    google_auth.TOKEN_FILE = tmp / "token.json"  # never the real one
    check_real_exchange()  # before _exchange_code is replaced below
    web.log.setLevel(logging.ERROR)
    llm.check_server = lambda: {"model_present": True, "version": "test", "model": llm.MODEL}
    google_auth.connected_email = lambda user_id=None: OWN
    auth._exchange_code = fake_exchange
    auth._verify_id_token = fake_verify
    auth.revoke = fake_revoke
    agent.run_turn = fake_run_turn
    return tmp


def sign_in(client: TestClient, code: str):
    """Start at /auth/login, then come back the way Google does (GET /?code=&state=)."""
    r = client.get("/auth/login", follow_redirects=False)
    loc = r.headers.get("location", "")
    state = (parse_qs(urlsplit(loc).query).get("state") or [""])[0]
    back = client.get("/", params={"code": code, "state": state, "scope": " ".join(auth.SIGNIN_SCOPES)}, follow_redirects=False)
    return r, back


def me(client: TestClient) -> dict:
    return client.get("/me").json()


def refused(r) -> bool:
    return r.status_code == 302 and r.headers.get("location", "").startswith("/login?error=")


# --------------------------------------------------------------------------- checks


def check_real_exchange():
    """auth.start/_exchange_code with the real Flow; only the HTTP POST to Google is faked."""
    started = auth.start(f"{BASE}/")
    seen: dict = {}
    real_fetch = OAuth2Session.fetch_token

    def fake_fetch(self, token_url, **kw):
        seen.update(kw, url=token_url)
        return {"access_token": "at", "refresh_token": "rt", "id_token": "idt", "token_type": "Bearer", "expires_in": 3600,
                "scope": ["openid", *google_auth.SCOPES]}

    OAuth2Session.fetch_token = fake_fetch
    try:
        out = auth._exchange_code("the-code", f"{BASE}/", started["state"], started["code_verifier"])
    finally:
        OAuth2Session.fetch_token = real_fetch
    check("exchange posts code, PKCE verifier and client secret to Google's token URL",
          seen.get("code") == "the-code" and seen.get("code_verifier") == started["code_verifier"] and seen.get("client_secret") == "test-secret"
          and seen.get("url") == "https://oauth2.googleapis.com/token", {k: v for k, v in seen.items() if k != "client_secret"})
    check("exchange returns refresh token, id token, scopes", out == {"refresh_token": "rt", "id_token": "idt", "scopes": ["openid", *google_auth.SCOPES]}, out)


def check_store():
    blob = store.encrypt("1//secret-refresh")
    check("fernet round trip", store.decrypt(blob) == "1//secret-refresh")
    check("ciphertext hides the token", b"secret-refresh" not in blob)

    imp = store.upsert_user("import:boss@acme.example", "boss@acme.example", "Alex", "")
    check("import row created", imp["google_sub"] == "import:boss@acme.example" and imp["id"])
    check("first user is the import", store.first_user_id() == imp["id"])
    store.save_token(imp["id"], "rt-imported", google_auth.SCOPES)
    tok = store.get_token(imp["id"])
    check("token saved and read back", tok and tok["refresh_token"] == "rt-imported" and tok["scopes"] == google_auth.SCOPES, tok)
    with sqlite3.connect(store.DB_FILE) as conn:
        raw = conn.execute("SELECT refresh_token_enc FROM google_tokens WHERE user_id = ?", (imp["id"],)).fetchone()[0]
    check("refresh token is encrypted at rest", b"rt-imported" not in bytes(raw))

    key = os.environ["VAANI_TOKEN_KEY"]
    os.environ["VAANI_TOKEN_KEY"] = Fernet.generate_key().decode()
    try:
        store.get_token(imp["id"])
        check("wrong key is refused", False, "no StoreError")
    except store.StoreError as e:
        check("wrong key is refused", "sign in again" in str(e), e)
    os.environ["VAANI_TOKEN_KEY"] = key
    os.environ.pop("VAANI_TOKEN_KEY")
    try:
        store.encrypt("x")
        check("missing key is refused", False)
    except store.StoreError as e:
        check("missing key is refused", "init-env" in str(e), e)
    os.environ["VAANI_TOKEN_KEY"] = key

    other = store.upsert_user("sub-other", "other@acme.example", "Other", "")
    again = store.upsert_user("sub-other", "Other@Acme.example", "Other Renamed", "p")
    check("returning user keeps its id", again["id"] == other["id"] and again["name"] == "Other Renamed" and again["email"] == "other@acme.example", again)
    with sqlite3.connect(store.DB_FILE) as conn:
        n = conn.execute("SELECT COUNT(*) FROM users WHERE email = 'other@acme.example'").fetchone()[0]
    check("returning user adds no row", n == 1, n)
    check("new user gets a new id", other["id"] != imp["id"])
    check("get_user reads back", (store.get_user(other["id"]) or {}).get("email") == "other@acme.example")
    check("get_user_by_email is case-insensitive", (store.get_user_by_email("OTHER@acme.example") or {}).get("id") == other["id"])
    check("delete_token", store.delete_token(imp["id"]) and store.get_token(imp["id"]) is None)
    store.save_token(imp["id"], "rt-imported", google_auth.SCOPES)
    return imp


def check_memory(imp_id: str):
    a, b = "user-a-test", "user-b-test"
    memory.save(copy.deepcopy(memory.DEFAULT), a)
    memory.update(lambda d: d["facts"].append("A likes tea"), a)
    check("update persists for that user", "A likes tea" in memory.load(a)["facts"])
    check("other user does not see it", "A likes tea" not in memory.load(b)["facts"])
    check("single-user file untouched", memory.load(None)["facts"] == ["single fact"], memory.load(None)["facts"])
    check("per-user path", memory.path_for(a) == memory.USERS_DIR / a / "memory.json")

    def as_user(uid, fn, *args):
        def run():
            context.current_user.set({"id": uid, "email": "", "name": "", "picture": ""})
            return fn(*args)
        return contextvars.copy_context().run(run)

    as_user(b, memory.append_fact, "B fact")
    as_user(b, memory.add_contact, "Zed", "zed@acme.example")
    check("append_fact uses the current user", memory.load(b)["facts"] == ["B fact"] and "B fact" not in memory.load(a)["facts"])
    check("add_contact uses the current user", any(c["email"] == "zed@acme.example" for c in memory.load(b)["contacts"])
          and not any(c["email"] == "zed@acme.example" for c in memory.load(a)["contacts"]))
    check("load() with no user is the single file", memory.load()["name"] == "Alex")
    check("load() under a user is that user's", as_user(a, memory.load)["facts"] == ["A likes tea"])

    old = memory.USERS_DIR / "old-file" / "memory.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"name": "Old", "contacts": [], "facts": [], "autonomy": {"replies": {"level": "one_tap"}}, "followups": {"wait_days": 5}}))
    d = memory.load("old-file")
    check("old file gains new top-level keys", d["profile"] == {} and d["brief"] == {"days": 3}, d)
    check("old file gains nested keys and keeps its values",
          d["autonomy"]["replies"] == {"level": "one_tap", "send_unlocked": False, "snoozed_at": None}
          and d["autonomy"]["triage"]["level"] == "suggest" and d["followups"] == {"wait_days": 5, "dismissed": []}, d["autonomy"])
    try:
        memory.path_for("../escape")
        check("path traversal refused", False)
    except ValueError:
        check("path traversal refused", True)


def check_single_mode():
    os.environ["VAANI_AUTH"] = "single"
    c = TestClient(web.app, base_url=BASE)
    r = c.get("/me")
    body = r.json()
    check("single: /me works with no cookie", r.status_code == 200 and body["auth_mode"] == "single" and body["email"] == OWN, body)
    check("single: /me user and csrf are null", body["user"] is None and body["csrf"] is None and body["reauth_required"] is False, body)
    check("single: /me flags default off", body["flags"] == {"autonomy": False, "promotion": False, "brief_on_open": False, "followups": False, "theme": "classic"}, body["flags"])
    r = c.post("/new")
    check("single: /new works with no cookie", r.status_code == 200 and r.json().get("session_id"), r.text)
    sid = r.json().get("session_id")
    check("single: session has no owner", web.SESSIONS[sid].owner is None)
    r = c.post("/chat", json={"session_id": sid, "text": "hi"})
    check("single: /chat runs with no user", r.status_code == 200 and r.json()["reply"] == "None", r.text)
    check("single: no session cookie is set", "vaani_session" not in c.cookies)
    r = c.get("/", follow_redirects=False)
    check("single: GET / serves the page", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), r.status_code)
    check("single: /login goes to the app", c.get("/login", follow_redirects=False).headers.get("location") == "/")
    r = c.post("/chat", json={"session_id": "nope", "text": "hi"})
    check("single: unknown session is 404 JSON", r.status_code == 404 and "unknown session_id" in r.json().get("error", ""), r.text)
    r = c.post("/chat", json={"text": "hi"})
    check("single: malformed body is 422 JSON", r.status_code == 422 and r.json().get("error", "").startswith("bad request"), r.text)


def check_google_mode(imp_id: str):
    os.environ["VAANI_AUTH"] = "google"
    a = TestClient(web.app, base_url=BASE)
    r = a.post("/new")
    check("google: /new without a session is 401", r.status_code == 401 and r.json() == {"error": "sign in required"}, r.text)
    r = a.post("/chat", json={"session_id": "x", "text": "hi"})
    check("google: /chat without a session is 401", r.status_code == 401, r.status_code)
    r = a.get("/", follow_redirects=False)
    check("google: GET / redirects to /login", r.status_code == 302 and r.headers.get("location") == "/login", r.headers)
    body = me(a)
    check("google: anonymous /me has no user or email", body["auth_mode"] == "google" and body["user"] is None and body["csrf"] is None and body["email"] is None, body)
    r = a.get("/login")
    check("google: /login serves a sign-in page", r.status_code == 200 and "/auth/login" in r.text, r.status_code)
    check("google: static files are public", a.get("/static/index.html").status_code == 200)

    r = TestClient(web.app).get("/auth/login", follow_redirects=False)  # host "testserver"
    check("google: non-loopback host is refused", refused(r) and "loopback" in unquote(r.headers["location"]), r.headers.get("location"))

    r = a.get("/auth/login", follow_redirects=False)
    loc = urlsplit(r.headers.get("location", ""))
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    check("google: /auth/login redirects to Google", r.status_code == 302 and loc.netloc == "accounts.google.com", r.headers.get("location"))
    check("google: loopback root redirect_uri", q.get("redirect_uri") == f"{BASE}/", q.get("redirect_uri"))
    check("google: PKCE S256 challenge", q.get("code_challenge_method") == "S256" and len(q.get("code_challenge", "")) >= 43, q)
    check("google: offline + consent + granted scopes", q.get("access_type") == "offline" and q.get("prompt") == "consent" and q.get("include_granted_scopes") == "true", q)
    check("google: scopes include openid, email, gmail, calendar",
          all(s in q.get("scope", "").split() for s in ["openid", "https://www.googleapis.com/auth/userinfo.email", *google_auth.SCOPES]), q.get("scope"))
    check("google: client id is the Desktop client", q.get("client_id") == CLIENT_ID)
    state = q.get("state", "")
    r = a.get("/", params={"code": "codeA", "state": "forged"}, follow_redirects=False)
    check("google: bad state is refused", refused(r) and not EXCHANGES, r.headers.get("location"))
    r = a.get("/", params={"code": "codeA", "state": state}, follow_redirects=False)
    check("google: state is single use", refused(r) and not EXCHANGES, r.headers.get("location"))
    check("google: refused callback signs nobody in", me(a)["user"] is None)
    r = a.get("/", params={"error": "access_denied", "state": "s"}, follow_redirects=False)
    check("google: consent cancelled is refused", refused(r) and "access_denied" in unquote(r.headers["location"]), r.headers.get("location"))

    _, back = sign_in(a, "codeA")
    check("google: good callback redirects to /", back.status_code == 302 and back.headers.get("location") == "/", (back.status_code, back.headers.get("location")))
    check("google: code exchanged with PKCE verifier and same redirect", EXCHANGES and EXCHANGES[-1]["redirect_uri"] == f"{BASE}/" and len(EXCHANGES[-1]["verifier"] or "") >= 43, EXCHANGES[-1:])
    body = me(a)
    user_a = body.get("user") or {}
    csrf_a = body.get("csrf") or ""
    check("google: /me returns the user", user_a.get("email") == "boss@acme.example" and user_a.get("name") == "Alex Boss" and body["email"] == "boss@acme.example", body)
    check("google: /me returns a csrf token", len(csrf_a) >= 32, csrf_a)
    check("google: import row claimed by real sign-in", user_a.get("id") == imp_id and (store.get_user(imp_id) or {}).get("google_sub") == "sub-A", user_a)
    check("google: new refresh token stored", (store.get_token(imp_id) or {}).get("refresh_token") == "rt-codeA")
    mem_a = memory.load(imp_id)
    check("google: first user inherits name, timezone, contacts", mem_a["name"] == "Alex" and mem_a["timezone"] == "Asia/Kolkata"
          and mem_a["contacts"] == [{"name": "Ravi Mehta", "email": "ravi@acme.example"}] and mem_a["facts"] == [], mem_a)
    check("google: profile saved", mem_a["profile"] == {"email": "boss@acme.example", "picture": "https://pic/a"}, mem_a["profile"])
    check("google: session cookie set", "vaani_session" in a.cookies)
    check("google: GET / now serves the page", a.get("/", follow_redirects=False).status_code == 200)
    check("google: /login now goes to the app", a.get("/login", follow_redirects=False).headers.get("location") == "/")

    r = a.post("/new")
    check("google: POST without CSRF is 403", r.status_code == 403 and "X-CSRF-Token" in r.json().get("error", ""), r.text)
    r = a.post("/new", headers={"X-CSRF-Token": "wrong"})
    check("google: POST with wrong CSRF is 403", r.status_code == 403, r.status_code)
    h_a = {"X-CSRF-Token": csrf_a}
    r = a.post("/new", headers=h_a)
    sid_a = r.json().get("session_id")
    check("google: POST /new with CSRF works", r.status_code == 200 and sid_a, r.text)
    check("google: session records its owner", web.SESSIONS[sid_a].owner == imp_id)
    r = a.post("/chat", json={"session_id": sid_a, "text": "hi"}, headers=h_a)
    check("google: turn runs as the user (context set in the worker)", r.status_code == 200 and {k: r.json().get(k) for k in ("reply", "who")} == {"reply": imp_id, "who": "Alex"}, r.text)

    b = TestClient(web.app, base_url=BASE)
    sign_in(b, "codeB")
    body_b = me(b)
    user_b = body_b.get("user") or {}
    h_b = {"X-CSRF-Token": body_b.get("csrf") or ""}
    check("google: second user gets a new account", user_b.get("email") == "priya@acme.example" and user_b.get("id") not in (None, imp_id), user_b)
    check("google: csrf differs per user", body_b.get("csrf") and body_b.get("csrf") != csrf_a)
    mem_b = memory.load(user_b.get("id"))
    check("google: later user starts from DEFAULT with given name", mem_b["name"] == "Priya" and mem_b["contacts"] == memory.DEFAULT["contacts"], mem_b)
    r = b.post("/chat", json={"session_id": sid_a, "text": "hi"}, headers=h_b)
    check("google: user B cannot use user A's session (404)", r.status_code == 404 and "unknown session_id" in r.json().get("error", ""), r.text)
    sid_b = b.post("/new", headers=h_b).json().get("session_id")
    r = b.post("/chat", json={"session_id": sid_b, "text": "hi"}, headers=h_b)
    check("google: user B's turn runs as B", {k: r.json().get(k) for k in ("reply", "who")} == {"reply": user_b.get("id"), "who": "Priya"}, r.text)
    r = a.post("/chat", json={"session_id": sid_b, "text": "hi"}, headers=h_a)
    check("google: user A cannot use user B's session (404)", r.status_code == 404, r.status_code)
    r = b.post("/chat", json={"session_id": sid_b, "text": "hi"}, headers=h_a)
    check("google: A's csrf does not work in B's session", r.status_code == 403, r.status_code)

    os.environ["VAANI_ALLOWED_EMAILS"] = "boss@acme.example, priya@acme.example"
    c = TestClient(web.app, base_url=BASE)
    _, back = sign_in(c, "codeC")
    check("google: email not on the allow list is refused", refused(back) and me(c)["user"] is None and store.get_user_by_email("stranger@else.example") is None,
          back.headers.get("location"))
    os.environ["VAANI_ALLOWED_EMAILS"] = ""
    _, back = sign_in(c, "codeU")
    check("google: unverified email is refused", refused(back) and store.get_user_by_email("unverified@acme.example") is None, back.headers.get("location"))
    _, back = sign_in(c, "codeS")
    check("google: missing Gmail scope is refused", refused(back) and "gmail.modify" in unquote(back.headers.get("location", "")), back.headers.get("location"))
    _, back = sign_in(c, "codeX")
    check("google: Google refusing the code is refused, not a 500", refused(back), (back.status_code, back.headers.get("location")))

    real_ce = google_auth.connected_email

    def reauth_ce(user_id=None):
        raise google_auth.ReauthRequired("expired")

    google_auth.connected_email = reauth_ce
    check("google: /me reports reauth_required", me(a)["reauth_required"] is True)
    google_auth.connected_email = real_ce
    check("google: /me reauth_required false when Google works", me(a)["reauth_required"] is False)

    r = a.post("/auth/logout")
    check("google: logout without CSRF is 403", r.status_code == 403, r.status_code)
    r = a.post("/auth/logout", headers=h_a)
    check("google: logout ok", r.status_code == 200 and r.json() == {"ok": True}, r.text)
    check("google: logout clears the user", me(a)["user"] is None)
    check("google: after logout /new is 401", a.post("/new", headers=h_a).status_code == 401)
    check("google: logout drops the user's chat sessions", sid_a not in web.SESSIONS and sid_b in web.SESSIONS)

    r = b.post("/auth/disconnect", headers=h_b)
    check("google: disconnect revokes and forgets the token", r.status_code == 200 and r.json() == {"ok": True, "revoked": True}
          and REVOKED == ["rt-codeB"] and store.get_token(user_b["id"]) is None, (r.text, REVOKED))
    check("google: disconnect signs out", me(b)["user"] is None)
    check("google: disconnect keeps the account and memory", store.get_user(user_b["id"]) and memory.path_for(user_b["id"]).exists())
    return imp_id, user_b.get("id")


def check_google_auth(id_a: str, id_b: str):
    try:
        google_auth.get_services(id_b)
        check("no stored token raises ReauthRequired", False)
    except google_auth.ReauthRequired:
        check("no stored token raises ReauthRequired", google_auth.needs_reauth(id_b))
    real_refresh = Credentials.refresh

    def refused_refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    Credentials.refresh = refused_refresh
    try:
        google_auth.get_services(id_a)
        check("RefreshError becomes ReauthRequired", False)
    except google_auth.ReauthRequired as e:
        check("RefreshError becomes ReauthRequired", isinstance(e, RefreshError) and google_auth.needs_reauth(id_a), e)
    finally:
        Credentials.refresh = real_refresh
    built: dict = {}

    def fake_refresh(self, request):
        built["creds"] = self
        self.token = "access"

    Credentials.refresh = fake_refresh
    try:
        svc = google_auth.get_services(id_a)
        creds = built["creds"]
        check("user credentials come from the store and credentials.json",
              creds.refresh_token == "rt-codeA" and creds.client_id == CLIENT_ID and creds.client_secret == "test-secret", creds.client_id)
        check("services cached per user (and per thread)", google_auth.get_services(id_a) is svc and not google_auth.needs_reauth(id_a))
        import threading
        other: dict = {}
        t = threading.Thread(target=lambda: other.update(svc=google_auth.get_services(id_a)))
        t.start(); t.join()
        check("another thread gets its own clients (httplib2 is not thread-safe)", other.get("svc") is not None and other["svc"] is not svc)
        google_auth.forget(id_a)
        check("forget drops the cache", google_auth._user_creds_cache.get(id_a) is None and google_auth.get_services(id_a) is not svc)
    finally:
        Credentials.refresh = real_refresh


def check_cli_and_env(tmp: Path, id_a: str):
    def run():
        problem = cli._sign_in_as("BOSS@acme.example")
        return problem, context.user_id()

    problem, uid = contextvars.copy_context().run(run)
    check("cli --user sets the current user", problem is None and uid == id_a, (problem, uid))
    problem, _ = contextvars.copy_context().run(lambda: (cli._sign_in_as("nobody@x.example"), None))
    check("cli --user unknown email is refused", problem and "no Vaani account" in problem, problem)
    check("cli without --user stays single", context.user_id() is None)

    env = tmp / ".env"
    env.write_text("OLLAMA_MODEL=qwen2.5:7b\nVAANI_TOKEN_KEY=\n")
    auth.ENV_FILE = env
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        auth.main(["init-env"])
    text = env.read_text()
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    check("init-env fills an empty key in place and appends the secret", text.count("VAANI_TOKEN_KEY=") == 1
          and values.get("OLLAMA_MODEL") == "qwen2.5:7b" and len(values.get("VAANI_SESSION_SECRET", "")) >= 40, text.replace(values.get("VAANI_SESSION_SECRET", "~"), "<s>"))
    try:
        Fernet(values.get("VAANI_TOKEN_KEY", "").encode())
        check("init-env token key is a valid Fernet key", True)
    except ValueError as e:
        check("init-env token key is a valid Fernet key", False, e)
    check("init-env never prints secrets", values["VAANI_TOKEN_KEY"] not in out.getvalue() and values["VAANI_SESSION_SECRET"] not in out.getvalue(), out.getvalue())
    check("init-env is idempotent", auth.init_env(env) == [] and env.read_text() == text)
    check("init-env makes .env private", (env.stat().st_mode & 0o077) == 0, oct(env.stat().st_mode))


def main() -> int:
    tmp = setup()
    imp = check_store()
    check_memory(imp["id"])
    check_single_mode()
    id_a, id_b = check_google_mode(imp["id"])
    check_google_auth(id_a, id_b)
    check_cli_and_env(tmp, id_a)
    os.environ["VAANI_AUTH"] = "single"

    failed = [x for x in CHECKS if not x[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL  {name}\n      {detail[:400]}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} auth checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
