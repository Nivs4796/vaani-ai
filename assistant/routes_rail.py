"""Left-rail data: today's meetings and the email drafts Vaani saved (owner: RAIL).

GET /meetings/today  the signed-in user's primary-calendar events for today, in their time zone
GET /drafts/recent   email drafts Vaani saved to Gmail for this user in the last 24 hours

Both are read-only and never fail the page: a Google or log problem comes back as HTTP 200
with an empty list and an "error" line. Drafts cost no Google call: they are the successful
commit_pending rows with variant save_draft in the tail of logs/tools.jsonl (the log only
grows, so only its end is read). Subject and recipient come from the row itself when it
carries them, else from the chat session that made the card while it is still in memory;
a draft whose session is gone shows as "Email draft". The page's static/rail.js renders both.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from assistant import context, google_auth, memory, obs

router = APIRouter()
log = logging.getLogger("assistant.web")

MEETINGS_MAX = 50
WHERE_MAX = 80
TAIL_LINES = 3000
DRAFTS_MAX = 10
DRAFTS_WINDOW = timedelta(hours=24)
GMAIL_DRAFTS = "https://mail.google.com/mail/#drafts"
# A Gmail API message id is lowercase hex; only such an id is ever put into a link.
GMAIL_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


# --------------------------------------------------------------------------- meetings


def _local_iso(d: dict | None, tz) -> tuple[str, bool, datetime | None]:
    """Google start/end -> (ISO local time with offset, all_day, aware datetime or None)."""
    d = d or {}
    if d.get("date"):
        return str(d["date"]), True, None
    raw = str(d.get("dateTime") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        dt = dt.astimezone(tz)
        return dt.isoformat(timespec="minutes"), False, dt
    except ValueError:
        return raw, False, None


def _where(ev: dict) -> str:
    place = " ".join(str(ev.get("location") or "").split())
    if not place:
        solution = ((ev.get("conferenceData") or {}).get("conferenceSolution") or {}).get("name")
        place = str(solution or ("Google Meet" if ev.get("hangoutLink") else ""))
    return place if len(place) <= WHERE_MAX else place[: WHERE_MAX - 1].rstrip() + "…"


def _https(link) -> str | None:
    s = str(link or "").strip()
    return s if s.startswith("https://") else None


def today_meetings() -> dict:
    """Today's events (start of day to end of day, user's time zone), all-day first, then by start."""
    from assistant import tools

    tz = tools._tz()
    day = datetime.now(tz).date()
    out = {"date": day.isoformat(), "count": 0, "events": []}
    try:
        lo = datetime.combine(day, time.min, tz)
        hi = datetime.combine(day + timedelta(days=1), time.min, tz)
        cal = google_auth.get_services()["calendar"]
        resp = (
            cal.events()
            .list(calendarId="primary", timeMin=lo.isoformat(), timeMax=hi.isoformat(), singleEvents=True,
                  orderBy="startTime", maxResults=MEETINGS_MAX)
            .execute(num_retries=2)
        )
        rows = []
        for ev in (resp or {}).get("items") or []:
            if not isinstance(ev, dict) or ev.get("status") == "cancelled":
                continue
            start, all_day, start_dt = _local_iso(ev.get("start"), tz)
            end, _, _ = _local_iso(ev.get("end"), tz)
            if all_day:
                # Google's all-day end date is exclusive; show the last day, as tools.list_calendar_events does.
                try:
                    end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
                except ValueError:
                    pass
            rows.append(({"id": ev.get("id"), "title": " ".join(str(ev.get("summary") or "").split()) or "(no title)",
                          "start": start, "end": end, "all_day": all_day, "where": _where(ev),
                          "link": _https(ev.get("htmlLink"))},
                         (not all_day, start_dt.timestamp() if start_dt else 0.0)))
        rows.sort(key=lambda r: r[1])
        out["events"] = [r[0] for r in rows]
        out["count"] = len(out["events"])
    except google_auth.ReauthRequired:
        out["error"] = "Google needs you to sign in again to show your calendar."
    except Exception as e:  # the rail shows an empty section; the chat still works
        log.warning("meetings/today failed: %s: %s", type(e).__name__, e)
        out["error"] = f"Could not read your calendar ({type(e).__name__})."
    return out


@router.get("/meetings/today")
async def meetings_today(request: Request):
    from assistant import web

    try:
        return await web._call_as(web._user(request), today_meetings)
    except Exception as e:  # backstop: never a 500
        log.exception("meetings/today crashed")
        return {"date": None, "count": 0, "events": [], "error": f"Could not read your calendar ({type(e).__name__})."}


# --------------------------------------------------------------------------- drafts saved


def tail_lines(path: Path, n: int = TAIL_LINES, block: int = 64 * 1024) -> list[str]:
    """The last n lines of a text file, read backwards in blocks."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos, data = f.tell(), b""
            while pos > 0 and data.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]


def _ts(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def saved_rows(lines: list[str], uid: str | None, now: datetime) -> list[dict]:
    """This user's saved email drafts from tools.jsonl lines: successful commit_pending rows with
    variant save_draft (only email cards have it), in the window, one per card, newest first."""
    since = now - DRAFTS_WINDOW
    found: dict[tuple, dict] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("tool") != "commit_pending" or row.get("ok") is not True:
            continue
        if row.get("user_id") != uid or row.get("card_kind") not in (None, "email_send"):
            continue
        try:
            args = json.loads(row.get("args") or "{}")
        except ValueError:
            args = {}
        args = args if isinstance(args, dict) else {}
        if (row.get("variant") or args.get("variant")) != "save_draft":
            continue
        ts = _ts(row.get("ts"))
        if ts is None or not since <= ts <= now + timedelta(minutes=5):
            continue
        key = (row.get("session_id"), row.get("draft_id") or args.get("draft_id"))
        if key not in found or ts > found[key]["ts"]:
            found[key] = {"ts": ts, "row": row, "session_id": key[0], "draft_id": key[1]}
    return sorted(found.values(), key=lambda r: r["ts"], reverse=True)[:DRAFTS_MAX]


def _session_draft(uid: str | None, session_id, draft_id) -> dict:
    """The card from the chat session that saved it (owner-checked); {} once the session is gone."""
    from assistant import web

    entry = web.SESSIONS.get(str(session_id or ""))
    if entry is None or entry.owner != uid:
        return {}
    draft = (getattr(entry.session, "drafts", None) or {}).get(draft_id)
    return draft if isinstance(draft, dict) else {}


def _to_text(to, names: dict[str, str]) -> str:
    addrs = [str(a).strip() for a in (to if isinstance(to, list) else [to]) if str(a or "").strip()]
    if not addrs:
        return ""
    first = names.get(addrs[0].lower(), addrs[0])
    return first + (f" +{len(addrs) - 1}" if len(addrs) > 1 else "")


def _draft_link(message_id) -> str:
    mid = str(message_id or "").strip()
    return f"{GMAIL_DRAFTS}?compose={mid}" if GMAIL_ID_RE.match(mid) else GMAIL_DRAFTS


def recent_drafts(now: datetime | None = None) -> dict:
    uid = context.user_id()
    rows = saved_rows(tail_lines(obs.TOOLS_LOG), uid, now or datetime.now(timezone.utc))
    try:
        names = {str(c.get("email") or "").lower(): str(c.get("name") or "").strip()
                 for c in memory.load().get("contacts") or [] if isinstance(c, dict) and c.get("email") and c.get("name")}
    except Exception:
        names = {}
    drafts = []
    for r in rows:
        row, card = r["row"], _session_draft(uid, r["session_id"], r["draft_id"])
        committed = card.get("committed") if isinstance(card.get("committed"), dict) else {}
        subject = " ".join(str(row.get("subject") or card.get("subject") or "").split())
        drafts.append({"title": subject or "Email draft", "to": _to_text(row.get("to") or card.get("to"), names),
                       "when": r["ts"].isoformat(timespec="seconds"),
                       "link": _draft_link(row.get("message_id") or committed.get("message_id"))})
    return {"count": len(drafts), "drafts": drafts}


@router.get("/drafts/recent")
async def drafts_recent(request: Request):
    from assistant import web

    try:
        return await web._call_as(web._user(request), recent_drafts)
    except Exception as e:  # backstop: never a 500
        log.exception("drafts/recent crashed")
        return {"count": 0, "drafts": [], "error": f"Could not read the drafts log ({type(e).__name__})."}
