"""Replay evals/cases.jsonl through the real agent loop and check which TOOLS
were called (from TurnResult.tool_calls, never from the model's prose).

Each case: {"id", "setup": [{"role", "content"}, ...], "prompt",
            "expect_tools": [...], "forbid_tools": [...], "expect_text_regex": optional,
            "expect_draft_attendees": optional list, "expect_pending_kind": optional}

Setup turns are appended to the transcript without a model call (user messages
get the same [now: ...] tag run_turn adds), then `prompt` runs as one turn.

This runner NEVER takes the confirm path, so create_calendar_event is code-gated
to "NOT CREATED" even if the model calls it; a belt-and-braces guard below makes
that a hard failure instead of a Google write. It does read the live inbox and
calendar through the real tools.

Run:  .venv/bin/python -m evals.run            (exit 1 if any case fails)
      .venv/bin/python -m evals.run inbox_brief past_date   (subset by id)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assistant import agent, llm, tools  # noqa: E402

CASES_FILE = Path(__file__).resolve().parent / "cases.jsonl"


class EvalWriteAttempt(RuntimeError):
    """Raised if anything in an eval would reach events.insert."""


# agent._run_tool turns any exception from a tool into an ERROR result, so the
# guard also records the attempt here and run_case raises AFTER run_turn returns.
WRITE_ATTEMPTS: list[str] = []


def _guard_create():
    """Wrap create_calendar_event so an eval can never confirm a draft."""
    real = tools.REGISTRY["create_calendar_event"]

    def guarded(draft_id="", **kw):
        session = agent.obs.current_session.get()
        if session is not None and session.confirmed_draft_id is not None:
            WRITE_ATTEMPTS.append(f"session {session.id} had confirmed draft {session.confirmed_draft_id!r} when create_calendar_event({draft_id!r}) ran")
            raise EvalWriteAttempt("eval session has a confirmed draft; refusing to run create_calendar_event")
        return real(draft_id, **kw)

    tools.REGISTRY["create_calendar_event"] = guarded

    real_commit = tools.REGISTRY["commit_pending"]

    def guarded_commit(draft_id="", variant="", **kw):
        WRITE_ATTEMPTS.append(f"commit_pending({draft_id!r}) ran during an eval")
        raise EvalWriteAttempt("evals never confirm a card; refusing to run commit_pending")

    tools.REGISTRY["commit_pending"] = guarded_commit


def load_cases(only: set[str]) -> list[dict]:
    cases = []
    for line in CASES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        case = json.loads(line)
        if only and case["id"] not in only:
            continue
        cases.append(case)
    return cases


def seed(session: agent.Session, setup: list[dict]) -> None:
    for m in setup or []:
        role = m.get("role", "user")
        content = str(m.get("content", ""))
        if role == "user":
            content = f"{agent._now_tag()} {content}"
        session.messages.append({"role": role, "content": content})


def run_case(case: dict) -> tuple[bool, str]:
    session = agent.new_session()
    seed(session, case.get("setup") or [])
    r = agent.run_turn(session, case["prompt"])
    if WRITE_ATTEMPTS:
        raise EvalWriteAttempt("; ".join(WRITE_ATTEMPTS))
    seen = [t["name"] for t in r.tool_calls]
    seen_ok = {t["name"] for t in r.tool_calls if t["ok"]}
    problems = []
    for name in case.get("expect_tools") or []:
        if name not in seen:
            problems.append(f"expected {name}")
        elif name not in seen_ok:  # called, but it returned ERROR (Google down, token expired)
            errs = [t["error"] for t in r.tool_calls if t["name"] == name and not t["ok"]]
            problems.append(f"expected {name} failed: {(errs or ['?'])[0]}")
    for name in case.get("forbid_tools") or []:
        if name in seen:
            problems.append(f"forbidden {name} was called")
    want_att = case.get("expect_draft_attendees")
    if want_att is not None:
        pend = agent.pending_draft(session)
        got_att = sorted((pend or {}).get("attendees") or [])
        if not pend:
            problems.append("expected a pending draft")
        elif got_att != sorted(a.lower() for a in want_att):
            problems.append(f"draft attendees {got_att} != {sorted(want_att)}")
    want_kind = case.get("expect_pending_kind")
    if want_kind is not None:
        pend = agent.pending_draft(session)
        if not pend or pend.get("kind") != want_kind:
            problems.append(f"expected a pending {want_kind} card, got {(pend or {}).get('kind')}")
    rx = case.get("expect_text_regex")
    if rx and not re.search(rx, r.reply or ""):
        problems.append(f"reply did not match /{rx}/")
    if not (r.reply or "").strip():
        problems.append("empty reply")
    created = [d for d in session.drafts.values() if d.get("event_id")]
    if created:
        raise EvalWriteAttempt(f"an event was created during an eval: {created}")
    if r.reply.startswith("I can't reach the local model"):
        problems.append("model unreachable")
    detail = f"tools={seen or '[]'} {r.footer}"
    if problems:
        detail += f" | {'; '.join(problems)} | reply={r.reply[:160]!r}"
    return not problems, detail


def main(argv: list[str]) -> int:
    only = set(argv)
    try:
        info = llm.check_server()
        if not info["model_present"]:
            print(f"model {info['model']} is not in `ollama list`; run: ollama pull {info['model']}")
            return 1
    except llm.LLMError as e:
        print(f"cannot run evals: {e}")
        return 1
    _guard_create()
    cases = load_cases(only)
    if not cases:
        print(f"no cases matched in {CASES_FILE}")
        return 1
    failures = 0
    for case in cases:
        try:
            passed, detail = run_case(case)
        except EvalWriteAttempt as e:
            print(f"ABORT {case['id']}: {e}")
            return 2
        failures += 0 if passed else 1
        print(f"{'PASS' if passed else 'FAIL'}  {case['id']:<16} {detail}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
