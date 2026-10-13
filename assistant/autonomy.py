"""Per-workflow autonomy ladder (Off, Suggest, One-tap, Auto), decided in code.

Each workflow (replies, scheduling, follow_ups, triage) has a level in the user's
memory. decide() turns a card plus that level into "wait" (the card's own button),
"inline" (a one-tap approve) or "auto" (the web layer confirms it with
obs.current_confirmer = "policy"). The model never sees any of this: its tools and
prompt are unchanged, and every write still goes through commit_pending or
create_calendar_event with Google's read-back.

Promotion is earned, never assumed: streak() reads logs/tools.jsonl and counts this
user's clean approvals in a row (carried out, verified, not edited, confirmed by the
user). At VAANI_PROMOTION_N of them offer_for() proposes one level up, never past the
ceiling: One-tap, or Auto for triage (reversible mailbox changes only) and for a
workflow whose sending the user unlocked, and only for workflows in
VAANI_AUTO_ALLOWED. A failed or unverified write at Auto drops the workflow back to
One-tap at once (on_commit_result).

Flags, read at call time: VAANI_AUTONOMY=0 (default) makes decide() always "wait";
VAANI_PROMOTION=0 (default) means no offers; VAANI_PROMOTION_N (default 5);
VAANI_AUTO_ALLOWED (default triage).

CLI: python -m assistant.autonomy show|reset [--user <user id or email>]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone

from assistant import context, memory, obs

WORKFLOWS = ("replies", "scheduling", "follow_ups", "triage")
LEVELS = ("off", "suggest", "one_tap", "auto")
WORKFLOW_TITLES = {"replies": "Replies", "scheduling": "Scheduling", "follow_ups": "Follow-ups", "triage": "Triage"}
LEVEL_TITLES = {"off": "Off", "suggest": "Suggest", "one_tap": "One-tap", "auto": "Auto"}
_NOUNS = {"replies": "replies", "scheduling": "scheduling", "follow_ups": "follow-ups", "triage": "triage"}

# Mailbox changes that can be undone with one opposite change. Trash is reversible for
# 30 days too, but it is the one that loses mail if nobody notices, so it is never Auto.
REVERSIBLE = frozenset({"archive", "mark_read", "label", "unarchive", "mark_unread", "unlabel", "star", "unstar"})
NEVER_AUTO = frozenset({"trash", "untrash"})
# "undo": the Undo button on a receipt carried out the opposite change; like "policy", it is
# no evidence for the streak either way (the undo_applied row is what ends the streak).
CONFIRMERS = ("user", "inline", "policy", "undo")
# The opposite mailbox change for Undo. Label changes are left out: the brief's triage cards
# never set a label, and an unlabel needs the label name.
INVERSE = {"archive": "unarchive", "unarchive": "archive", "mark_read": "mark_unread", "mark_unread": "mark_read",
           "star": "unstar", "unstar": "star", "trash": "untrash", "untrash": "trash"}
# Skipped cards in a row at One-tap or Auto before Vaani offers to step that workflow back.
STEPBACK_SKIPS = 2
COMMIT_TOOLS = ("commit_pending", "create_calendar_event")
# Rows that end a streak: a skipped card, and any level change (the next offer needs
# fresh evidence at the new level).
RESET_TOOLS = ("skip_pending", "promotion_accept", "promotion_snooze", "level_change", "undo_applied")
# Fields tag_draft() adds; tools.draft_calendar_event keeps them out of the model's result.
DRAFT_TAGS = ("workflow", "origin", "proposed_hash", "edited", "revision_of", "drafted_turn")
# What a card would actually do; a change to any of these after drafting is an edit.
_CONTENT_KEYS = ("to", "cc", "subject", "body", "thread_id", "action", "ids", "label_name", "target_event_id",
                 "patch", "response", "summary", "start", "end", "attendees", "description")
PROOF_KEYS = ("ts", "workflow", "tool", "card_kind", "confirmed_by", "edited", "verified", "ok", "ms", "level")
_PROOF_EXTRA = ("origin", "draft_id", "variant", "decision", "from_level", "to_level", "by", "reason", "streak", "error")

_offers_logged: set[tuple] = set()
_offers_lock = threading.Lock()

# --------------------------------------------------------------------------- flags


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _flag("VAANI_AUTONOMY")


def promotion_enabled() -> bool:
    return _flag("VAANI_PROMOTION")


def promotion_n() -> int:
    try:
        return max(1, int(os.environ.get("VAANI_PROMOTION_N", "5").strip()))
    except ValueError:
        return 5


def auto_allowed() -> tuple[str, ...]:
    raw = os.environ.get("VAANI_AUTO_ALLOWED", "triage")
    return tuple(w for w in (x.strip().lower() for x in raw.split(",")) if w in WORKFLOWS)


# --------------------------------------------------------------------------- prefs


def _uid(user_id: str | None) -> str | None:
    return user_id if user_id is not None else context.user_id()


def _wf_prefs(prefs, workflow: str) -> dict:
    """One workflow's prefs from the memory dict, its "autonomy" dict, or the workflow dict itself."""
    src = prefs if isinstance(prefs, dict) else {}
    if isinstance(src.get("autonomy"), dict):
        src = src["autonomy"]
    raw = src if ("level" in src and workflow not in src) else src.get(workflow)
    out = {"level": "suggest", "send_unlocked": False, "snoozed_at": None}
    if isinstance(raw, dict):
        out.update({k: raw[k] for k in out if k in raw})
    if out["level"] not in LEVELS:
        out["level"] = "suggest"
    out["send_unlocked"] = bool(out["send_unlocked"])
    return out


def _prefs(user_id: str | None = None) -> dict:
    data = memory.load(_uid(user_id))
    return {wf: _wf_prefs(data, wf) for wf in WORKFLOWS}


def _set_prefs(workflow: str, user_id: str | None, **fields) -> None:
    def apply(data: dict) -> None:
        auto = data.get("autonomy")
        if not isinstance(auto, dict):
            auto = data["autonomy"] = {}
        cur = auto.get(workflow)
        if not isinstance(cur, dict):
            cur = auto[workflow] = _wf_prefs({}, workflow)
        cur.update(fields)

    memory.update(apply, _uid(user_id))


def level_of(workflow: str, user_id: str | None = None) -> str:
    try:
        return _prefs(user_id)[workflow]["level"]
    except Exception:
        return "suggest"


def ceiling_for(workflow: str, prefs: dict) -> str:
    """The highest level a workflow can reach: Auto only for triage or an unlocked sender,
    and only when the server allows Auto for that workflow at all."""
    p = _wf_prefs(prefs, workflow)
    if workflow in auto_allowed() and (workflow == "triage" or p["send_unlocked"]):
        return "auto"
    return "one_tap"


def _check(workflow: str) -> str:
    wf = str(workflow or "").strip().lower()
    if wf not in WORKFLOWS:
        raise ValueError(f"unknown workflow {workflow!r}; use one of {', '.join(WORKFLOWS)}")
    return wf


# --------------------------------------------------------------------------- drafts


def workflow_of(draft: dict) -> str:
    kind = (draft or {}).get("kind") or "event_create"
    if kind == "email_send":
        return "follow_ups" if draft.get("origin") == "followup" else "replies"
    if kind == "email_modify":
        return "triage"
    return "scheduling"  # event_create, event_update, event_delete, event_rsvp


def proposed_hash(draft: dict) -> str:
    content = {k: draft.get(k) for k in _CONTENT_KEYS if k in draft}
    content["kind"] = draft.get("kind") or "event_create"
    raw = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def tag_draft(draft: dict, prev: dict | None = None) -> dict:
    """Add workflow, origin, proposed_hash and the edit flag to a new draft, in place.

    prev is the session's pending draft this one replaces. A replacement of a
    still-pending card of the same workflow from an earlier turn is the user revising
    it ("yes, but make it 4pm"), so the new card counts as edited. Call before prev is
    marked SUPERSEDED."""
    session = obs.current_session.get()
    turn = getattr(session, "turn", None)
    draft["origin"] = str(draft.get("origin") or obs.current_origin.get() or "asked")
    draft["workflow"] = workflow_of(draft)
    draft["proposed_hash"] = proposed_hash(draft)
    draft["drafted_turn"] = turn
    revising = (
        isinstance(prev, dict)
        and prev.get("status") == "DRAFT"
        and (prev.get("workflow") or workflow_of(prev)) == draft["workflow"]
        # The model re-drafting inside one turn is not the user editing anything.
        and (prev.get("drafted_turn") is None or prev.get("drafted_turn") != turn)
    )
    draft["edited"] = bool(revising)
    if revising:
        draft["revision_of"] = prev.get("draft_id")
    return draft


def was_edited(draft: dict) -> bool:
    if draft.get("edited"):
        return True
    h = draft.get("proposed_hash")
    return bool(h) and proposed_hash(draft) != h


def confirmer() -> str:
    who = obs.current_confirmer.get()
    return who if who in CONFIRMERS else "user"


def commit_fields(draft: dict, variant: str = "") -> dict:
    """Decision fields for the commit row (commit_pending / create_calendar_event)."""
    wf = draft.get("workflow") or workflow_of(draft)
    fields = {
        "workflow": wf,
        "origin": draft.get("origin") or "asked",
        "card_kind": draft.get("kind") or "event_create",
        "confirmed_by": confirmer(),
        "edited": was_edited(draft),
        "level": level_of(wf),
        "draft_id": draft.get("draft_id"),
    }
    if variant:
        fields["variant"] = variant
    if draft.get("decision"):
        fields["decision"] = draft["decision"]
    return fields


def verified_of(result: dict | None) -> bool:
    """Whether Google's read-back confirmed a committed card."""
    if not isinstance(result, dict):
        return False
    if "verified" in result:
        return bool(result["verified"])
    if "verified_count" in result:
        return bool(result.get("count")) and result.get("verified_count") == result.get("count")
    return result.get("status") == "CREATED"


# --------------------------------------------------------------------------- policy


def decide(draft: dict, prefs: dict | None = None) -> str:
    """wait | inline | auto. Code, never the model, picks how a card is confirmed."""
    if not enabled() or not isinstance(draft, dict):
        return "wait"
    wf = draft.get("workflow") or workflow_of(draft)
    if (draft.get("origin") or "asked") == "asked":
        return "wait"  # the user asked for it in the chat: they confirm it themselves
    p = _wf_prefs(prefs if prefs is not None else memory.load(), wf)
    if p["level"] in ("off", "suggest"):
        return "wait"
    if p["level"] == "one_tap":
        return "inline"
    action = draft.get("action")
    if draft.get("kind") == "email_modify" and action in NEVER_AUTO:
        return "inline"
    reversible = draft.get("kind") == "email_modify" and action in REVERSIBLE
    allowed = wf in auto_allowed() and (reversible or p["send_unlocked"])
    return "auto" if allowed else "inline"


# --------------------------------------------------------------------------- streaks and offers


def _parse_ts(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rows(user_id: str | None) -> list[dict]:
    """This user's decision rows from logs/tools.jsonl, oldest first. No file: no rows."""
    uid = _uid(user_id)
    out: list[dict] = []
    try:
        with open(obs.TOOLS_LOG, encoding="utf-8") as f:
            for line in f:
                if '"workflow"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a half-written last line
                if isinstance(row, dict) and row.get("user_id") == uid and row.get("workflow") in WORKFLOWS:
                    out.append(row)
    except OSError:
        return []
    return out


def _is_commit_row(row: dict) -> bool:
    return row.get("tool") in COMMIT_TOOLS and "card_kind" in row


def _streak(rows: list[dict], workflow: str, snoozed_at) -> int:
    since = _parse_ts(snoozed_at) if snoozed_at else None
    n = 0
    for row in rows:
        if row.get("workflow") != workflow:
            continue
        if since is not None:
            t = _parse_ts(row.get("ts"))
            if t is None or t <= since:
                continue
        tool = row.get("tool")
        if tool in RESET_TOOLS:
            n = 0
            continue
        if not _is_commit_row(row) or row.get("confirmed_by") in ("policy", "undo"):
            continue  # Auto's own writes, and Undo's, prove nothing about the user's trust
        clean = (row.get("ok") is True and row.get("verified") is True and not row.get("edited")
                 and row.get("confirmed_by") in ("user", "inline"))
        n = n + 1 if clean else 0
    return n


def streak(workflow: str, user_id: str | None = None) -> int:
    wf = _check(workflow)
    p = _prefs(user_id)[wf]
    return _streak(_rows(user_id), wf, p["snoozed_at"])


def _offer(workflow: str, p: dict, n: int) -> dict | None:
    if not promotion_enabled() or p["level"] == "off":
        return None
    i = LEVELS.index(p["level"])
    if i >= LEVELS.index(ceiling_for(workflow, p)) or n < promotion_n():
        return None
    to = LEVELS[i + 1]
    noun = _NOUNS[workflow]
    text = (f"Vaani has been right {n} times in a row on {noun} (no edits, all verified). "
            f"Move {noun} from {LEVEL_TITLES[p['level']]} to {LEVEL_TITLES[to]}?")
    return {"workflow": workflow, "from_level": p["level"], "to_level": to, "streak": n, "text": text}


def _state_row(workflow: str, p: dict, n: int) -> dict:
    need = promotion_n()
    return {
        "key": workflow,
        "title": WORKFLOW_TITLES[workflow],
        "level": p["level"],
        "send_unlocked": p["send_unlocked"],
        "streak": n,
        "n_needed": need,
        "remaining": max(0, need - n),
        "ceiling": ceiling_for(workflow, p),
        "offer_due": _offer(workflow, p, n) is not None,
        "auto_allowed": workflow in auto_allowed(),
        "snoozed_at": p["snoozed_at"],
    }


def get_state(user_id: str | None = None) -> dict:
    """{workflow: {key, title, level, send_unlocked, streak, n_needed, remaining, ceiling,
    offer_due, auto_allowed, snoozed_at}} for GET /autonomy (list(get_state().values()))."""
    prefs = _prefs(user_id)
    rows = _rows(user_id)
    return {wf: _state_row(wf, prefs[wf], _streak(rows, wf, prefs[wf]["snoozed_at"])) for wf in WORKFLOWS}


def state_for(workflow: str, user_id: str | None = None) -> dict:
    wf = _check(workflow)
    p = _prefs(user_id)[wf]
    return _state_row(wf, p, _streak(_rows(user_id), wf, p["snoozed_at"]))


def offer_for(workflow: str, user_id: str | None = None) -> dict | None:
    """{workflow, from_level, to_level, streak, text} when a promotion is due, else None."""
    wf = str(workflow or "").strip().lower()
    if wf not in WORKFLOWS or not promotion_enabled():
        return None
    uid = _uid(user_id)
    p = _prefs(uid)[wf]
    offer = _offer(wf, p, _streak(_rows(uid), wf, p["snoozed_at"]))
    if offer is not None:
        key = (uid, wf, offer["from_level"], offer["streak"])
        with _offers_lock:
            fresh = key not in _offers_logged
            _offers_logged.add(key)
        if fresh:
            obs.log_decision("promotion_offer", user_id=uid, workflow=wf, from_level=offer["from_level"],
                             to_level=offer["to_level"], streak=offer["streak"], level=offer["from_level"])
    return offer


def accept_offer(workflow: str, user_id: str | None = None) -> dict:
    """Raise the workflow one level (only when an offer is due). Returns its state row."""
    wf = _check(workflow)
    uid = _uid(user_id)
    offer = offer_for(wf, uid)
    if offer is None:
        raise ValueError(f"no promotion is due for {_NOUNS[wf]}")
    _set_prefs(wf, uid, level=offer["to_level"])
    obs.log_decision("promotion_accept", user_id=uid, workflow=wf, from_level=offer["from_level"],
                     to_level=offer["to_level"], streak=offer["streak"], level=offer["to_level"], by="user")
    return state_for(wf, uid)


def snooze_offer(workflow: str, user_id: str | None = None) -> dict:
    """"Not now": the next offer counts only clean approvals after this moment."""
    wf = _check(workflow)
    uid = _uid(user_id)
    p = _prefs(uid)[wf]
    row = obs.log_decision("promotion_snooze", user_id=uid, workflow=wf, level=p["level"],
                           streak=_streak(_rows(uid), wf, p["snoozed_at"]))
    _set_prefs(wf, uid, snoozed_at=row["ts"])
    return state_for(wf, uid)


def set_level(workflow: str, level: str | None = None, send_unlocked: bool | None = None,
              user_id: str | None = None) -> dict:
    """Set a workflow's level and/or send unlock. Raises ValueError when Auto is not allowed."""
    wf = _check(workflow)
    uid = _uid(user_id)
    p = _prefs(uid)[wf]
    new = dict(p)
    if send_unlocked is not None:
        new["send_unlocked"] = bool(send_unlocked)
    if level is not None:
        lv = str(level).strip().lower().replace("-", "_")
        if lv not in LEVELS:
            raise ValueError(f"unknown level {level!r}; use one of {', '.join(LEVELS)}")
        new["level"] = lv
    noun = _NOUNS[wf]
    if new["level"] == "auto" and ceiling_for(wf, new) != "auto":
        if level is None:
            new["level"] = "one_tap"  # sending was locked again: Auto can no longer send
        elif wf not in auto_allowed():
            allowed = ", ".join(auto_allowed()) or "none"
            raise ValueError(f"Auto is not allowed for {noun} on this server (allowed: {allowed})")
        else:
            raise ValueError(f"Auto for {noun} is locked: unlock sending first, so Vaani may send without a click")
    _set_prefs(wf, uid, level=new["level"], send_unlocked=new["send_unlocked"])
    if new["level"] != p["level"]:
        obs.log_decision("level_change", user_id=uid, workflow=wf, from_level=p["level"], to_level=new["level"],
                         level=new["level"], send_unlocked=new["send_unlocked"], by="user", reason="set")
    return state_for(wf, uid)


# --------------------------------------------------------------------------- outcomes


def record_skip(draft: dict, user_id: str | None = None) -> dict | None:
    """Log skip_pending for a card the user skipped or cancelled (ends that workflow's streak).
    Call it before cancelling; a carried-out card or a second call logs nothing."""
    if not isinstance(draft, dict) or draft.get("committed") or draft.get("event_id") or draft.get("skip_logged"):
        return None
    wf = draft.get("workflow") or workflow_of(draft)
    draft["skip_logged"] = True
    return obs.log_decision("skip_pending", user_id=_uid(user_id), workflow=wf, origin=draft.get("origin") or "asked",
                            card_kind=draft.get("kind") or "event_create", draft_id=draft.get("draft_id"),
                            level=level_of(wf, user_id), decision=draft.get("decision"))


def undo_plan(draft: dict | None) -> dict | None:
    """{action, ids, count} for the opposite change of a carried-out mailbox card, or None
    (not a mailbox card, not carried out, already undone, itself an undo, or a label change)."""
    if not isinstance(draft, dict) or draft.get("kind") != "email_modify" or not draft.get("committed"):
        return None
    if draft.get("origin") == "undo" or draft.get("undone"):
        return None
    inverse = INVERSE.get(str(draft.get("action") or ""))
    ids = [str(i) for i in draft.get("ids") or [] if str(i).strip()]
    if not inverse or not ids:
        return None
    return {"action": inverse, "ids": ids, "count": len(ids)}


def record_undo(draft: dict, user_id: str | None = None) -> dict | None:
    """The user undid a carried-out card: it was wrong, so the workflow's streak ends, and if
    Vaani did it at Auto the workflow steps down to One-tap. Never raises."""
    try:
        wf = draft.get("workflow") or workflow_of(draft)
        uid = _uid(user_id)
        level = _prefs(uid)[wf]["level"]
        row = obs.log_decision("undo_applied", user_id=uid, workflow=wf, card_kind=draft.get("kind"),
                               draft_id=draft.get("draft_id"), level=level, decision=draft.get("decision"))
        if level == "auto":
            _set_prefs(wf, uid, level="one_tap")
            obs.log_decision("level_change", user_id=uid, workflow=wf, from_level="auto", to_level="one_tap",
                             level="one_tap", by="safety", reason="undone", draft_id=draft.get("draft_id"))
        return row
    except Exception:
        return None


def stepback_for(workflow: str, user_id: str | None = None) -> dict | None:
    """After STEPBACK_SKIPS skipped cards in a row at One-tap or Auto: an offer to step the
    workflow back one level. Vaani never lowers trust silently on skips; the user decides."""
    wf = str(workflow or "").strip().lower()
    if wf not in WORKFLOWS or not enabled():
        return None
    uid = _uid(user_id)
    level = _prefs(uid)[wf]["level"]
    if level not in ("one_tap", "auto"):
        return None
    skips = 0
    for row in reversed(_rows(uid)):
        if row.get("workflow") != wf:
            continue
        tool = row.get("tool")
        if tool == "skip_pending":
            skips += 1
            if skips >= STEPBACK_SKIPS:
                break
            continue
        if tool in ("level_change", "promotion_accept", "stepback_keep"):
            break  # fresh evidence is needed after any decision about the level
        if _is_commit_row(row) and row.get("confirmed_by") not in ("policy", "undo"):
            break  # an approval in between: the skips were not in a row
    if skips < STEPBACK_SKIPS:
        return None
    to = LEVELS[LEVELS.index(level) - 1]
    noun = _NOUNS[wf]
    return {"kind": "step_back", "workflow": wf, "from_level": level, "to_level": to, "skips": skips,
            "text": f"You skipped the last {skips} {noun} cards. Step {noun} back from {LEVEL_TITLES[level]} to {LEVEL_TITLES[to]}?",
            "accept_action": "step_back", "accept_label": f"Step back to {LEVEL_TITLES[to]}",
            "snooze_action": "keep", "snooze_label": f"Keep {LEVEL_TITLES[level]}"}


def accept_stepback(workflow: str, user_id: str | None = None) -> dict:
    """Lower the workflow one level (only when a step-back offer is due). Returns its state row."""
    wf = _check(workflow)
    uid = _uid(user_id)
    offer = stepback_for(wf, uid)
    if offer is None:
        raise ValueError(f"no step-back is due for {_NOUNS[wf]}")
    _set_prefs(wf, uid, level=offer["to_level"])
    obs.log_decision("level_change", user_id=uid, workflow=wf, from_level=offer["from_level"], to_level=offer["to_level"],
                     level=offer["to_level"], by="user", reason="step_back")
    return state_for(wf, uid)


def keep_level(workflow: str, user_id: str | None = None) -> dict:
    """"Keep One-tap": no step-back offer until STEPBACK_SKIPS more skips in a row."""
    wf = _check(workflow)
    uid = _uid(user_id)
    obs.log_decision("stepback_keep", user_id=uid, workflow=wf, level=_prefs(uid)[wf]["level"])
    return state_for(wf, uid)


def on_commit_result(draft: dict, result_ok: bool, verified: bool, user_id: str | None = None) -> str | None:
    """Safety demotion: a failed or unverified write while the workflow is at Auto drops it
    to One-tap at once. Returns the new level, or None when nothing changed. Never raises."""
    try:
        if result_ok and verified:
            return None
        wf = draft.get("workflow") or workflow_of(draft)
        uid = _uid(user_id)
        if _prefs(uid)[wf]["level"] != "auto":
            return None
        _set_prefs(wf, uid, level="one_tap")
        obs.log_decision("level_change", user_id=uid, workflow=wf, from_level="auto", to_level="one_tap",
                         level="one_tap", by="safety", reason="failed" if not result_ok else "unverified",
                         draft_id=draft.get("draft_id"))
        return "one_tap"
    except Exception:
        return None


def proof(limit: int = 20, workflow: str | None = None, user_id: str | None = None) -> list[dict]:
    """Recent decision rows for the Proof panel, newest first."""
    try:
        n = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        n = 20
    wf = str(workflow or "").strip().lower() or None
    out: list[dict] = []
    for row in reversed(_rows(user_id)):
        if wf and row.get("workflow") != wf:
            continue
        if row.get("tool") in COMMIT_TOOLS and not _is_commit_row(row):
            continue
        item = {k: row.get(k) for k in PROOF_KEYS}
        item.update({k: row[k] for k in _PROOF_EXTRA if row.get(k) is not None})
        out.append(item)
        if len(out) >= n:
            break
    return out


# --------------------------------------------------------------------------- CLI


def _cli_user(arg: str | None) -> str | None:
    if not arg:
        return None
    if "@" in arg:
        from assistant import store  # only the CLI needs it

        user = store.get_user_by_email(arg)
        if not user:
            raise SystemExit(f"no signed-in user with email {arg}")
        return str(user["id"])
    return arg


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    uid = _cli_user(argv[argv.index("--user") + 1] if "--user" in argv[:-1] else None)
    if cmd == "reset":
        # Demo prep: every workflow back to Suggest, sending locked, streaks start from now.
        for wf in WORKFLOWS:
            before = _prefs(uid)[wf]["level"]
            row = obs.log_decision("level_change", user_id=uid, workflow=wf, from_level=before, to_level="suggest",
                                   level="suggest", by="reset", reason="reset")
            _set_prefs(wf, uid, level="suggest", send_unlocked=False, snoozed_at=row["ts"])
    elif cmd != "show":
        print("usage: python -m assistant.autonomy show|reset [--user <user id or email>]")
        return 2
    for row in get_state(uid).values():
        print(f"{row['title']:<11} {LEVEL_TITLES[row['level']]:<8} streak {row['streak']}/{row['n_needed']}  "
              f"ceiling {LEVEL_TITLES[row['ceiling']]}{'  unlocked' if row['send_unlocked'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
