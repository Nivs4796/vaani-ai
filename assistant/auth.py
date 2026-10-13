"""Sign in with Google (VAANI_AUTH=google): one Vaani account per Google user.

Uses the Desktop ("installed") OAuth client already in credentials.json, so no
Google Cloud Console change is needed: Desktop clients accept any loopback
redirect, and we redirect to the root of whatever loopback address the page was
opened on (http://127.0.0.1:8010/ or http://localhost:8010/). Authorization-code
flow with PKCE; state and code_verifier live in the signed session cookie.

The Google network calls are isolated in _exchange_code, _verify_id_token and
revoke so the offline tests can replace them.

CLI:
  python -m assistant.auth init-env      add VAANI_TOKEN_KEY and VAANI_SESSION_SECRET to .env if missing
  python -m assistant.auth import-token  turn today's token.json into the first Vaani account
"""

from __future__ import annotations

import copy
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from assistant import google_auth, memory, store

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)

# Google may hand back the scopes in another order, or with earlier grants added
# (include_granted_scopes); oauthlib would otherwise raise on the difference.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SIGNIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    *google_auth.SCOPES,
]
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class AuthError(Exception):
    """A sign-in that must be refused; the message is safe to show the user."""


def mode() -> str:
    """"google" when VAANI_AUTH=google, else "single" (today's behaviour)."""
    return "google" if os.environ.get("VAANI_AUTH", "single").strip().lower() == "google" else "single"


def allowed_emails() -> set[str]:
    raw = os.environ.get("VAANI_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def public(user: dict | None) -> dict | None:
    """The four fields that go into context.current_user, the session cookie and GET /me."""
    if not user:
        return None
    return {k: user.get(k) or "" for k in ("id", "email", "name", "picture")}


def redirect_uri_for(scheme: str, netloc: str) -> str:
    """Root of the address the page was opened on. Refuses non-loopback hosts, which a
    Desktop OAuth client cannot redirect to."""
    host = (urlsplit(f"//{netloc}").hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise AuthError(
            f"Google sign-in only works when Vaani is opened on a loopback address (http://127.0.0.1:PORT), not {netloc}"
        )
    return f"{scheme}://{netloc}/"


def _flow(redirect_uri: str, state: str | None = None, code_verifier: str | None = None):
    from google_auth_oauthlib.flow import Flow

    config = {"installed": google_auth.client_info()}
    return Flow.from_client_config(
        config,
        scopes=SIGNIN_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=True,
    )


def start(redirect_uri: str) -> dict:
    """Build the Google consent URL. Returns {"url", "state", "code_verifier"}; the caller keeps
    state and code_verifier in the session for finish()."""
    flow = _flow(redirect_uri)
    url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return {"url": url, "state": state, "code_verifier": flow.code_verifier}


def _scope_list(scope) -> list[str]:
    if isinstance(scope, str):
        return scope.split()
    return [str(s) for s in (scope or [])]


def _exchange_code(code: str, redirect_uri: str, state: str, code_verifier: str) -> dict:
    """Network: trade the code (plus PKCE verifier) for tokens. {"refresh_token", "id_token", "scopes"}."""
    flow = _flow(redirect_uri, state=state, code_verifier=code_verifier)
    token = flow.fetch_token(code=code)  # Google's token response, as a dict
    return {
        "refresh_token": token.get("refresh_token"),
        "id_token": token.get("id_token"),
        "scopes": _scope_list(token.get("scope")),
    }


def _verify_id_token(raw: str, client_id: str) -> dict:
    """Network (Google's signing certs): verify signature, issuer, audience and expiry; return the claims."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(raw, Request(), client_id, clock_skew_in_seconds=10)


def revoke(refresh_token: str) -> bool:
    """Network: ask Google to revoke this grant. True when Google confirmed it."""
    import requests

    try:
        r = requests.post(REVOKE_URL, data={"token": refresh_token}, timeout=10,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        return r.status_code == 200
    except requests.RequestException:
        return False


def ensure_memory(user: dict, given_name: str = "") -> dict:
    """Create the user's memory on first sign-in, and refresh its profile on every sign-in.

    The first account inherits name, timezone and contacts from data/memory.json (today's
    single-user memory); everyone else starts from DEFAULT with their Google given name.
    """
    uid = user["id"]
    profile = {"email": user.get("email") or "", "picture": user.get("picture") or ""}
    if not memory.path_for(uid).exists():
        data = copy.deepcopy(memory.DEFAULT)
        if store.first_user_id() == uid:
            single = memory.load_path(memory.MEMORY_FILE)
            for key in ("name", "timezone", "contacts"):
                data[key] = copy.deepcopy(single.get(key, data[key]))
        else:
            first = (user.get("name") or "").split()
            data["name"] = given_name or (first[0] if first else "") or (user.get("email") or "").split("@")[0] or data["name"]
        data["profile"] = profile
        memory.save(data, uid)
        return data
    return memory.update(lambda d: d.__setitem__("profile", profile), uid)


def finish(code: str, redirect_uri: str, state: str, code_verifier: str) -> dict:
    """Complete sign-in after the caller has matched `state` against the session.

    Returns the public user dict. Raises AuthError with a user-facing message on refusal.
    """
    if not code:
        raise AuthError("Google did not send a sign-in code; please try again")
    client_id = google_auth.client_info().get("client_id") or ""
    try:
        tok = _exchange_code(code, redirect_uri, state, code_verifier)
    except AuthError:
        raise
    except Exception as e:  # oauthlib raises many types; none should reach the page as a 500
        raise AuthError(f"Google refused the sign-in code ({type(e).__name__}); please sign in again") from e
    if not tok.get("id_token"):
        raise AuthError("Google did not return an ID token; please sign in again")
    try:
        claims = _verify_id_token(tok["id_token"], client_id)
    except Exception as e:
        raise AuthError(f"could not verify Google's ID token ({type(e).__name__}); please sign in again") from e
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise AuthError("Google has not verified this account's email address, so Vaani cannot use it")
    allowed = allowed_emails()
    if allowed and email not in allowed:
        raise AuthError(f"{email} is not allowed on this Vaani (VAANI_ALLOWED_EMAILS)")
    missing = [s.rsplit("/", 1)[-1] for s in google_auth.SCOPES if s not in set(tok.get("scopes") or [])]
    if missing:
        raise AuthError(f"Vaani needs Gmail and Calendar access ({', '.join(missing)} was not granted); sign in again and leave every box ticked")

    user = store.upsert_user(str(claims["sub"]), email, str(claims.get("name") or email), str(claims.get("picture") or ""))
    if tok.get("refresh_token"):
        store.save_token(user["id"], tok["refresh_token"], tok["scopes"])
    elif store.get_token(user["id"]) is None:
        raise AuthError("Google did not return offline access; remove Vaani at myaccount.google.com/permissions and sign in again")
    ensure_memory(user, given_name=str(claims.get("given_name") or ""))
    google_auth.forget(user["id"])
    return public(user)


# --------------------------------------------------------------------------- CLI


def _env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    return key.removeprefix("export ").strip(), value.strip().strip("'\"")


def init_env(path: Path | None = None) -> list[str]:
    """Give .env a VAANI_TOKEN_KEY and VAANI_SESSION_SECRET when they are missing or empty
    (an empty `KEY=` line, as copied from .env.example, is filled in place). Returns the names set."""
    from cryptography.fernet import Fernet

    path = Path(path or ENV_FILE)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    fresh = {"VAANI_TOKEN_KEY": Fernet.generate_key().decode(), "VAANI_SESSION_SECRET": secrets.token_urlsafe(48)}
    have = {kv[0]: kv[1] for kv in map(_env_line, lines) if kv}
    todo = [k for k in fresh if not have.get(k)]
    if not todo:
        return []
    for i, line in enumerate(lines):
        kv = _env_line(line)
        if kv and kv[0] in todo and not kv[1]:
            lines[i] = f"{kv[0]}={fresh[kv[0]]}"
            have[kv[0]] = fresh[kv[0]]
    lines += [f"{k}={fresh[k]}" for k in todo if not have.get(k)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return todo


def import_token() -> dict:
    """Create the first Vaani account from token.json (single-user mode's Google token)."""
    creds = google_auth.get_creds()
    if not creds.refresh_token:
        raise AuthError(f"{google_auth.TOKEN_FILE} has no refresh token; run `python -m assistant.google_auth` first")
    email = google_auth.connected_email(None).strip().lower()
    existing = store.get_user_by_email(email)
    if existing:
        user = existing
    else:
        name = str(memory.load_path(memory.MEMORY_FILE).get("name") or email)
        user = store.upsert_user(f"import:{email}", email, name, "")
    store.save_token(user["id"], creds.refresh_token, list(creds.scopes or google_auth.SCOPES))
    ensure_memory(user)
    return public(user)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else ""
    if cmd == "init-env":
        added = init_env()
        for k in ("VAANI_TOKEN_KEY", "VAANI_SESSION_SECRET"):
            print(f"{k}: {'added to' if k in added else 'already in'} {ENV_FILE}")
        if added:
            print("Restart the server so it picks them up. Keep .env private: anyone with VAANI_TOKEN_KEY can read stored Google tokens.")
        return 0
    if cmd == "import-token":
        try:
            user = import_token()
        except (AuthError, store.StoreError) as e:
            print(f"ERROR: {e}")
            return 1
        print(f"Imported {user['email']} as Vaani user {user['id']}")
        print(f"Memory: {memory.path_for(user['id'])}")
        return 0
    print("Usage:\n" + __doc__.split("CLI:", 1)[-1].strip("\n").rstrip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
