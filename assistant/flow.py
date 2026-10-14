"""Demo flow on top of agent.run_turn: the brief on chat open, brief row actions,
the autonomy policy (wait / inline / auto) and promotion offers.

Code makes every decision here. The model only writes text inside run_turn, and
every write still runs through the existing gate (commit_pending or
create_calendar_event) with a read-back from Google.

Each public function returns the dict the web page gets: a TurnResult as a dict
plus "decision", "offer" and "receipt" (None when they do not apply).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from assistant import agent, autonomy, brief, followups, manage, memory, obs


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def brief_on_open() -> bool:
    return _flag("VAANI_BRIEF_ON_OPEN")


def _out(res: agent.TurnResult, **extra) -> dict:
    out = asdict(res)
    out.update({"decision": None, "offer": None, "receipt": None})
    out.update(extra)
    return out


def public_brief(b: dict | None) -> dict | None:
    if not b:
        return None
    return {k: v for k, v in b.items() if k != "tool_result"}


# --------------------------------------------------------------------------- brief on open

def open_chat(session: agent.Session) -> dict | None:
    """Build the brief in code and put it into the transcript the same way the typed
    "check my latest emails" request does, so "What does Ravi want?" works next."""
    if not brief_on_open():
        return None
    b = brief.build()
    if b.get("error") or not b.get("tool_result"):
        session.brief = b
        return public_brief(b)
    days = b.get("covers_days") or 3
    args = {"max_results": 20, "query": f"in:inbox newer_than:{days}d"}
    session.messages.append({"role": "user", "content": f"{agent._now_tag()} Check my latest emails and give me updates."})
    session.messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_recent_emails", "arguments": args}}]})
    session.messages.append({"role": "tool", "content": b["tool_result"], "tool_name": "list_recent_emails"})
    session.messages.append({"role": "assistant", "content": b.get("text") or "Here is your inbox brief."})
    session.brief = b
    return public_brief(b)


def refresh_brief(session: agent.Session | None) -> dict:
    b = brief.build(refresh=True)
    if session is not None:
        session.brief = b
    return public_brief(b) or {}


def _row(session: agent.Session, group: str, key: str, value: str) -> dict | None:
    b = getattr(session, "brief", None) or {}
    for g in b.get("groups") or []:
        if group and g.get("key") != group:
            continue
        for r in g.get("rows") or []:
            if value and str(r.get(key)) == str(value):
                return r
    return None


def _brief_ids(session: agent.Session, group: str) -> list[str]:
    """Message ids of one group in the brief this chat showed (all of them, not only the rows on screen)."""
    raw = (getattr(session, "brief", None) or {}).get("tool_result")
    try:
        rows = json.loads(raw).get("messages") or [] if raw else []
    except (TypeError, ValueError):
        return []
    return [str(r["id"]) for r in rows if r.get("group") == group and r.get("id")]


# --------------------------------------------------------------------------- receipts, offers, policy

def receipt(draft: dict | None) -> dict | None:
    """What Google confirmed for a carried-out card, or None if nothing ran."""
    if not draft:
        return None
    done = draft.get("committed")
    if done:
        rec = {"status": done.get("status"), "verified": bool(done.get("verified", done.get("verified_count"))),
               "report": list(done.get("report") or []), "link": done.get("htmlLink")}
        plan = autonomy.undo_plan(draft)
        if plan:
            words = plan["action"].replace("_", " ")
            rec["undo"] = {"draft_id": draft.get("draft_id"), "label": "Undo",
                           "text": f"Undo puts it back: {words} {plan['count']} email(s), checked with Gmail."}
        return rec
    created = draft.get("created")
    if draft.get("event_id") and created:
        status = created.get("status") or "CREATED"
        return {"status": status, "verified": status == "CREATED",
                "report": [f"Created: {draft.get('summary')}"], "link": created.get("htmlLink")}
    return None


def _offer(draft: dict | None) -> dict | None:
    wf = (draft or {}).get("workflow")
    if not wf:
        return None
    try:
        return autonomy.offer_for(wf)
    except Exception:
        return None


def _level(wf: str | None) -> str | None:
    if not wf:
        return None
    try:
        return (autonomy.get_state().get(wf) or {}).get("level")
    except Exception:
        return None


def apply_policy(session: agent.Session, res: agent.TurnResult) -> dict:
    """Ask the policy how the new card is confirmed. Auto confirms it here, through the
    same run_turn confirm path a click uses, with the confirmer recorded as "policy"."""
    card = res.pending_draft
    if not card:
        return _out(res)
    did = card.get("draft_id")
    draft = session.drafts.get(did) or {}
    try:
        decision = autonomy.decide(draft)
    except Exception:
        decision = "wait"
    draft["decision"] = decision
    out = _out(res, decision=decision)
    if out.get("pending_draft") is not None:
        out["pending_draft"]["decision"] = decision
        out["pending_draft"]["level"] = _level(draft.get("workflow"))
    if decision != "auto":
        return out
    token = obs.current_confirmer.set("policy")
    try:
        done = agent.run_turn(session, None, confirm_draft_id=did)
    finally:
        obs.current_confirmer.reset(token)
    rec = receipt(draft)
    if rec:
        brief.invalidate()
    return _out(done, decision="auto", receipt=rec)


# --------------------------------------------------------------------------- entry points used by web.py

def chat(session: agent.Session, text: str | None) -> dict:
    return apply_policy(session, agent.run_turn(session, text))


def confirm(session: agent.Session, draft_id: str, variant: str | None = None, confirmer: str | None = None) -> dict:
    """The card's button, or (confirmer="voice", from voice_policy.py) a spoken approval."""
    draft = session.drafts.get(str(draft_id))
    who = confirmer or ("inline" if (draft or {}).get("decision") == "inline" else "user")
    token = obs.current_confirmer.set(who)
    try:
        res = agent.run_turn(session, None, confirm_draft_id=str(draft_id), confirm_variant=variant)
    finally:
        obs.current_confirmer.reset(token)
    rec = receipt(draft)
    if rec and (draft or {}).get("origin") in ("brief", "followup"):
        brief.invalidate()  # the mailbox changed under the cached brief
    return _out(res, receipt=rec, offer=_offer(draft) if rec else None)


def skip(session: agent.Session, draft_id: str) -> dict:
    draft = session.drafts.get(str(draft_id))
    skipped = False
    if draft and draft.get("status") == "DRAFT":
        try:
            skipped = autonomy.record_skip(draft) is not None
        except Exception:
            pass
    offer = None
    if skipped:
        try:
            offer = autonomy.stepback_for(draft.get("workflow") or autonomy.workflow_of(draft))
        except Exception:
            offer = None
    return _out(agent.run_turn(session, None, cancel_draft_id=str(draft_id)), offer=offer)


def _code_turn(session: agent.Session, user_line: str, make, origin: str, t0: float) -> agent.TurnResult:
    """Run a card-making function in code inside the session, with the origin set,
    and keep the transcript coherent for the model's next turn."""
    session.turn += 1
    # The user line goes in first: the recipient rule reads addresses the user typed this chat.
    session.messages.append({"role": "user", "content": f"{agent._now_tag()} {user_line}"})
    s_tok = obs.current_session.set(session)
    o_tok = obs.current_origin.set(origin)
    try:
        result = make()
    finally:
        obs.current_origin.reset(o_tok)
        obs.current_session.reset(s_tok)
    ok = obs.result_ok(result)
    record = {"name": getattr(make, "__name__", "action"), "ms": 0, "ok": ok, "error": obs.error_text(result)}
    if ok and session.pending_draft_id:
        status = agent._status_line(agent.pending_draft(session))
        reply = f"Ready for you to check. {status}"
    else:
        reply = f"I could not prepare that: {obs.error_text(result) or result}"
    session.messages.append({"role": "assistant", "content": reply})
    return agent._result(session, reply, [record], agent._zero_usage(), t0)


def _undo(session: agent.Session, draft_id: str | None, t0: float) -> dict:
    """Receipt's Undo button: the opposite change as its own card, carried out at once through
    the same gate (the click is the confirmation) and read back from Gmail."""
    orig = session.drafts.get(str(draft_id or ""))
    plan = autonomy.undo_plan(orig)
    if plan is None:
        return _out(agent._result(session, "There is nothing to undo on that card.", [], agent._zero_usage(), t0))

    def draft_undo():
        return manage.draft_triage(plan["action"], origin="undo", message_ids=plan["ids"])

    words = plan["action"].replace("_", " ")
    res = _code_turn(session, f"Undo that: {words} the {plan['count']} email(s).", draft_undo, "undo", t0)
    card = res.pending_draft
    if not card:
        return _out(res)
    token = obs.current_confirmer.set("undo")
    try:
        done = agent.run_turn(session, None, confirm_draft_id=card["draft_id"])
    finally:
        obs.current_confirmer.reset(token)
    rec = receipt(session.drafts.get(card["draft_id"]))
    if rec and rec.get("verified"):
        orig["undone"] = True
        autonomy.record_undo(orig)
        brief.invalidate()
    return _out(done, decision="undo", receipt=rec)


def action(session: agent.Session, name: str, message_id: str | None = None, thread_id: str | None = None,
           draft_id: str | None = None) -> dict:
    """Brief row buttons: reply (asks the model what the sender wants, setting the email in
    focus), nudge (template follow-up in code), archive_all (triage card), dismiss; and the
    receipt's undo."""
    t0 = time.perf_counter()
    name = str(name or "")
    if name == "undo":
        return _undo(session, draft_id, t0)
    if name == "reply":
        row = _row(session, "", "id", message_id or "")
        who = ((row or {}).get("sender") or "").split()
        text = f"What does {who[0]} want?" if who else "Summarise the email I picked."
        return chat(session, text)
    if name == "nudge":
        row = _row(session, "follow_ups", "thread_id", thread_id or "")
        if row is None:
            rows = [r for r in followups.candidates() if str(r.get("thread_id")) == str(thread_id)]
            row = rows[0] if rows else None
        if row is None:
            return _out(agent._result(session, "That thread is no longer waiting on a reply.", [], agent._zero_usage(), t0))
        me = memory.load()
        body = followups.nudge_body(row, str(me.get("name") or ""))
        if _flag("VAANI_NUDGE_MODEL"):
            body = followups.rewrite_nudge(row, body, str(me.get("name") or ""))

        def draft_nudge():
            return manage.draft_nudge(row["message_id"], body)

        res = _code_turn(session, f"Draft a follow-up nudge to {row.get('to_name') or row.get('to_email')}.", draft_nudge, "followup", t0)
        return apply_policy(session, res)
    if name == "archive_all":
        ids = _brief_ids(session, "promotions")

        def draft_triage():
            # The exact promotions the brief listed, so the card's count matches what the user saw.
            return manage.draft_triage("archive", group="promotions", origin="brief", message_ids=ids)

        res = _code_turn(session, "Archive the promotions in my inbox.", draft_triage, "brief", t0)
        return apply_policy(session, res)
    if name == "dismiss":
        followups.dismiss(str(thread_id or ""))
        return _out(agent._result(session, "Hidden from Follow-ups. It will not come back.", [], agent._zero_usage(), t0))
    return _out(agent._result(session, f"Unknown action {name!r}; nothing was done.", [], agent._zero_usage(), t0))


EVAL_STATUS = Path(__file__).resolve().parent.parent / "data" / "eval_status.json"


def eval_status() -> dict | None:
    """The last `python -m evals.all` run (suites, pass counts, when), or None if it never ran."""
    try:
        data = json.loads(EVAL_STATUS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("suites") else None
    except (OSError, ValueError):
        return None


def proof(workflow: str | None = None, limit: int = 20) -> dict:
    rows = autonomy.proof(limit=limit, workflow=workflow or None)
    streaks = {}
    for wf in autonomy.WORKFLOWS:
        try:
            streaks[wf] = autonomy.streak(wf)
        except Exception:
            streaks[wf] = 0
    return {"rows": rows, "streaks": streaks, "evals": eval_status()}


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
