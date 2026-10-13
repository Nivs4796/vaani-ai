"""Google OAuth for the demo: installed-app flow, token cache, refresh recovery.

Run `python -m assistant.google_auth` once to do the browser consent and write
token.json. Every later import just reuses the cached token.

Testing-mode OAuth apps hand out refresh tokens that expire after 7 days.
When that happens the refresh raises RefreshError; we delete token.json and
re-run the consent flow instead of crashing.

Signed-in users (VAANI_AUTH=google, see auth.py) do not use token.json: their
refresh token lives encrypted in store.py, and a RefreshError raises
ReauthRequired so the web page can send them back through Google sign-in.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from assistant import context

# gmail.modify: read, send, draft, label, archive and trash, but Google does not let
# this scope permanently delete mail (that needs the full mail.google.com scope).
# calendar.events: read and write events on the user's own calendars.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = ROOT / "credentials.json"  # Desktop OAuth client from Google Cloud Console
TOKEN_FILE = ROOT / "token.json"  # written after the first consent; gitignored

# The API clients sit on httplib2, which is not thread-safe: two threadpool workers
# sharing one client corrupt each other's TLS stream ("SSLError: record layer failure").
# So credentials are cached once per user, and the clients built on them once per thread.
_single_creds: Credentials | None = None  # single-user mode (token.json)
_user_creds_cache: dict[str, Credentials] = {}  # signed-in users, by user id
_gen: dict[str, int] = {}  # bumped by forget()/reauth so every thread rebuilds its clients
_local = threading.local()
_user_emails: dict[str, str] = {}
_reauth: set[str] = set()  # user ids whose refresh token Google refused
_cache_lock = threading.Lock()


class ReauthRequired(RefreshError):
    """This user's stored Google token is missing, revoked or expired: sign in with Google again.

    A RefreshError subclass, so code that already catches RefreshError keeps working.
    """


def _load_cached() -> Credentials | None:
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    # A token minted for different scopes is useless; start over.
    if not set(SCOPES).issubset(set(creds.scopes or [])):
        TOKEN_FILE.unlink(missing_ok=True)
        return None
    return creds


def _save(creds: Credentials) -> None:
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)


def get_creds() -> Credentials:
    """Return valid credentials, refreshing or re-consenting as needed."""
    creds = _load_cached()
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except RefreshError:
            # Expired Testing-mode refresh token (7-day limit) or revoked access.
            TOKEN_FILE.unlink(missing_ok=True)
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"{CREDENTIALS_FILE} is missing. Download the Desktop OAuth client JSON "
            "from Google Cloud Console (Google Auth Platform > Clients) and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    # Opens the default browser; also prints the URL in case it does not.
    creds = flow.run_local_server(port=0, prompt="consent")
    _save(creds)
    return creds


def _build(creds: Credentials) -> dict:
    return {
        "gmail": build("gmail", "v1", credentials=creds, cache_discovery=False),
        "calendar": build("calendar", "v3", credentials=creds, cache_discovery=False),
    }


def client_info() -> dict:
    """client_id, client_secret, token_uri... of the Desktop ("installed") client in credentials.json."""
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(f"{CREDENTIALS_FILE} is missing; download the Desktop OAuth client JSON from Google Cloud Console.")
    config = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    return config.get("installed") or config.get("web") or {}


def _mark_reauth(user_id: str) -> None:
    with _cache_lock:
        _reauth.add(user_id)
        _user_creds_cache.pop(user_id, None)
        _gen[user_id] = _gen.get(user_id, 0) + 1
        _user_emails.pop(user_id, None)


class _UserCredentials(Credentials):
    """Credentials for one signed-in user: a refused refresh becomes ReauthRequired,
    whether it happens when the clients are built or an hour later mid-request."""

    def refresh(self, request):
        try:
            super().refresh(request)
        except RefreshError as e:
            uid = getattr(self, "_vaani_user_id", None)
            if uid:
                _mark_reauth(uid)
            raise ReauthRequired(f"Google refused this account's saved access ({e}); sign in with Google again") from e


def _user_creds(user_id: str) -> Credentials:
    from assistant import store  # local import: single-user mode never touches the database

    try:
        tok = store.get_token(user_id)
    except store.StoreError as e:
        _mark_reauth(user_id)
        raise ReauthRequired(str(e)) from None
    if not tok:
        _mark_reauth(user_id)
        raise ReauthRequired("no Google access is saved for this account; sign in with Google again")
    client = client_info()
    creds = _UserCredentials(
        token=None,
        refresh_token=tok["refresh_token"],
        token_uri=client.get("token_uri") or "https://oauth2.googleapis.com/token",
        client_id=client.get("client_id"),
        client_secret=client.get("client_secret"),
        scopes=tok["scopes"] or SCOPES,
    )
    creds._vaani_user_id = user_id
    creds.refresh(Request())  # fail now (ReauthRequired) rather than inside the first tool call
    return creds


def _thread_cache() -> dict:
    cache = getattr(_local, "services", None)
    if cache is None:
        cache = _local.services = {}
    return cache


def get_services(user_id: str | None = None) -> dict:
    """Gmail and Calendar API clients for this thread: per signed-in user, or from token.json."""
    global _single_creds
    uid = user_id if user_id is not None else context.user_id()
    key = uid or ""
    with _cache_lock:
        gen = _gen.get(key, 0)
    cached = _thread_cache().get(key)
    if cached is not None and cached[0] == gen:
        return cached[1]
    if uid is None:
        with _cache_lock:
            creds = _single_creds
        if creds is None:
            creds = get_creds()
            with _cache_lock:
                _single_creds = creds
    else:
        with _cache_lock:
            creds = _user_creds_cache.get(uid)
        if creds is None:
            creds = _user_creds(uid)
            with _cache_lock:
                _reauth.discard(uid)
                _user_creds_cache[uid] = creds
                gen = _gen.get(key, 0)
    services = _build(creds)
    _thread_cache()[key] = (gen, services)
    return services


def connected_email(user_id: str | None = None) -> str:
    uid = user_id if user_id is not None else context.user_id()
    if uid is None:
        profile = get_services()["gmail"].users().getProfile(userId="me").execute(num_retries=2)
        return profile["emailAddress"]
    with _cache_lock:
        if uid in _user_emails:
            return _user_emails[uid]
    profile = get_services(uid)["gmail"].users().getProfile(userId="me").execute(num_retries=2)
    with _cache_lock:
        _user_emails[uid] = profile["emailAddress"]
    return profile["emailAddress"]


def needs_reauth(user_id: str) -> bool:
    """True once Google refused this user's refresh token (cleared by the next successful build)."""
    with _cache_lock:
        return user_id in _reauth


def forget(user_id: str) -> None:
    """Drop this user's cached clients, e.g. after a new sign-in, a logout or a disconnect."""
    with _cache_lock:
        _user_creds_cache.pop(user_id, None)
        _gen[user_id] = _gen.get(user_id, 0) + 1
        _user_emails.pop(user_id, None)
        _reauth.discard(user_id)


if __name__ == "__main__":
    print(f"Connected as {connected_email()}")
    print(f"Scopes: {', '.join(s.rsplit('/', 1)[-1] for s in SCOPES)}")
    print(f"Token cached at {TOKEN_FILE}")
