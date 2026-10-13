"""Google OAuth for the demo: installed-app flow, token cache, refresh recovery.

Run `python -m assistant.google_auth` once to do the browser consent and write
token.json. Every later import just reuses the cached token.

Testing-mode OAuth apps hand out refresh tokens that expire after 7 days.
When that happens the refresh raises RefreshError; we delete token.json and
re-run the consent flow instead of crashing.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

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

_services: dict | None = None


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


def get_services() -> dict:
    """Cached Gmail and Calendar API clients, built once per process."""
    global _services
    if _services is None:
        creds = get_creds()
        _services = {
            "gmail": build("gmail", "v1", credentials=creds, cache_discovery=False),
            "calendar": build("calendar", "v3", credentials=creds, cache_discovery=False),
        }
    return _services


def connected_email() -> str:
    profile = get_services()["gmail"].users().getProfile(userId="me").execute(num_retries=2)
    return profile["emailAddress"]


if __name__ == "__main__":
    print(f"Connected as {connected_email()}")
    print(f"Scopes: {', '.join(s.rsplit('/', 1)[-1] for s in SCOPES)}")
    print(f"Token cached at {TOKEN_FILE}")
