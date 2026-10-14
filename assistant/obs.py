"""Observability: one JSONL line per tool call and per model call, plus the
session contextvar that tools use to find drafts and the confirm flag.

Every row carries the signed-in user's id (None in single-user mode). Decision rows
(log_decision, and the fields a tool adds with annotate()) are what autonomy.py reads
back to compute earned-promotion streaks, so the log is the single source of proof.

Postgres swap point: _write_line() is the only place a log row is persisted.
Replace its body with an INSERT and nothing else changes; the rows are already
column-shaped.
"""

from __future__ import annotations

import functools
import json
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from assistant import context

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
TOOLS_LOG = LOG_DIR / "tools.jsonl"
TURNS_LOG = LOG_DIR / "turns.jsonl"

# Set by agent.run_turn for the duration of a turn. Tools read it to reach the
# session's drafts and confirmed_draft_id. None means "no session" (direct call).
current_session: ContextVar = ContextVar("current_session", default=None)
# Where a card came from: "asked" (the user typed it), "brief" (a brief row button),
# "followup" (a follow-up nudge). Set by the code path that makes the card.
current_origin: ContextVar[str] = ContextVar("current_origin", default="asked")
# Who confirmed the card being carried out: "user" (the card's button), "inline"
# (One-tap approve on an inline card), "policy" (Auto), "undo" or "voice" (a spoken
# approval in voice mode, voice_policy.py). Set by the web layer.
current_confirmer: ContextVar[str] = ContextVar("current_confirmer", default="user")
# "voice" while a turn runs for a session in voice mode (routes_voiceturn.py sets it): the
# model then sees a shorter transcript (voicectx.trim). "chat" (the default) changes nothing.
current_mode: ContextVar[str] = ContextVar("current_mode", default="chat")
# The spoken approval being carried out ({"heard", "confidence", "tid"}) when current_confirmer
# is "voice"; commit_pending copies heard / confidence onto its decision row for Proof.
current_voice: ContextVar = ContextVar("current_voice", default=None)
# Extra fields for the row of the @logged call in progress (see annotate()).
_row_extra: ContextVar[dict | None] = ContextVar("_row_extra", default=None)
# A callable that hears every @logged call start and end ({"phase": "start"|"end", "tool",
# "ms", "ok"}). routes_stream.py sets it for one turn so the page can show live steps.
# None (the default) means nobody is listening and nothing changes.
step_sink: ContextVar = ContextVar("step_sink", default=None)

ARGS_MAX = 300
# A tool result that starts with one of these is a refusal/failure, never a success.
FAIL_PREFIXES = ("ERROR:", "NOT CREATED:", "NOT DONE:")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_line(path: Path, row: dict) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # Logging must never break a turn.
        pass


def result_ok(result) -> bool:
    """True unless the tool result is an ERROR:/NOT CREATED: string."""
    return not (isinstance(result, str) and result.lstrip().startswith(FAIL_PREFIXES))


def error_text(result) -> str | None:
    if result_ok(result):
        return None
    return str(result).strip()[:200]


def _session_fields() -> tuple[str | None, int | None]:
    s = current_session.get()
    if s is None:
        return None, None
    return getattr(s, "id", None), getattr(s, "turn", None)


def _base_row(tool: str, ms: int, ok: bool, error: str | None) -> dict:
    sid, turn = _session_fields()
    return {"ts": _ts(), "user_id": context.user_id(), "session_id": sid, "turn": turn, "tool": tool,
            "ms": ms, "ok": ok, "error": error}


def annotate(**fields) -> None:
    """Add fields to the log row of the @logged tool call in progress (no-op outside one).
    commit_pending and create_calendar_event use it so their own row is the decision row."""
    extra = _row_extra.get()
    if extra is not None:
        extra.update(fields)


def log_decision(tool: str, **fields) -> dict:
    """One pseudo-tool row (skip_pending, promotion_*, level_change) in logs/tools.jsonl,
    with the same base fields as a tool row. fields may override the base ones."""
    row = _base_row(tool, 0, True, None)
    row.update(fields)
    _write_line(TOOLS_LOG, row)
    return row


def _tell_sink(event: dict) -> None:
    sink = step_sink.get()
    if sink is None:
        return
    try:
        sink(event)
    except Exception:
        pass  # a listener must never break a tool call


def logged(fn):
    """Time a tool call and append one line to logs/tools.jsonl. Never raises:
    if the tool itself raises (they all catch, this is the backstop) the caller
    gets an ERROR string instead."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _tell_sink({"phase": "start", "tool": fn.__name__})
        t0 = time.perf_counter()
        extra_token = _row_extra.set({})
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            result = (
                f"ERROR: {fn.__name__} failed with {type(e).__name__}: {e}; "
                "nothing was changed; try again or rephrase the request"
            )
        finally:
            extra = _row_extra.get() or {}
            _row_extra.reset(extra_token)
        ms = int((time.perf_counter() - t0) * 1000)
        sid, turn = _session_fields()
        try:
            shown = {f"arg{i}": a for i, a in enumerate(args)}
            shown.update(kwargs)
            arg_text = json.dumps(shown, ensure_ascii=False, default=str)
        except Exception:
            arg_text = repr((args, kwargs))
        row = {
            "ts": _ts(),
            "user_id": context.user_id(),
            "session_id": sid,
            "turn": turn,
            "tool": fn.__name__,
            "args": arg_text[:ARGS_MAX],
            "ms": ms,
            "ok": result_ok(result),
            "error": error_text(result),
        }
        for key, value in extra.items():
            row.setdefault(key, value)  # the measured base fields always win
        _write_line(TOOLS_LOG, row)
        _tell_sink({"phase": "end", "tool": fn.__name__, "ms": ms, "ok": row["ok"]})
        return result

    return wrapper


def log_model_call(
    *,
    session_id: str | None,
    turn: int | None,
    model: str,
    prompt_tokens: int,
    gen_tokens: int,
    latency_ms: int,
    had_tool_calls: bool,
) -> None:
    """One line per ollama.chat call in logs/turns.jsonl."""
    _write_line(
        TURNS_LOG,
        {
            "ts": _ts(),
            "user_id": context.user_id(),
            "session_id": session_id,
            "turn": turn,
            "model": model,
            "prompt_tokens": int(prompt_tokens or 0),
            "gen_tokens": int(gen_tokens or 0),
            "latency_ms": int(latency_ms),
            "had_tool_calls": bool(had_tool_calls),
        },
    )
