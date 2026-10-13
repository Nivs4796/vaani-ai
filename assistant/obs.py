"""Observability: one JSONL line per tool call and per model call, plus the
session contextvar that tools use to find drafts and the confirm flag.

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

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
TOOLS_LOG = LOG_DIR / "tools.jsonl"
TURNS_LOG = LOG_DIR / "turns.jsonl"

# Set by agent.run_turn for the duration of a turn. Tools read it to reach the
# session's drafts and confirmed_draft_id. None means "no session" (direct call).
current_session: ContextVar = ContextVar("current_session", default=None)

ARGS_MAX = 300
# A tool result that starts with one of these is a refusal/failure, never a success.
FAIL_PREFIXES = ("ERROR:", "NOT CREATED:", "NOT DONE:")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_line(path: Path, row: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
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


def logged(fn):
    """Time a tool call and append one line to logs/tools.jsonl. Never raises:
    if the tool itself raises (they all catch, this is the backstop) the caller
    gets an ERROR string instead."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            result = (
                f"ERROR: {fn.__name__} failed with {type(e).__name__}: {e}; "
                "nothing was changed; try again or rephrase the request"
            )
        ms = int((time.perf_counter() - t0) * 1000)
        sid, turn = _session_fields()
        try:
            shown = {f"arg{i}": a for i, a in enumerate(args)}
            shown.update(kwargs)
            arg_text = json.dumps(shown, ensure_ascii=False, default=str)
        except Exception:
            arg_text = repr((args, kwargs))
        _write_line(
            TOOLS_LOG,
            {
                "ts": _ts(),
                "session_id": sid,
                "turn": turn,
                "tool": fn.__name__,
                "args": arg_text[:ARGS_MAX],
                "ms": ms,
                "ok": result_ok(result),
                "error": error_text(result),
            },
        )
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
            "session_id": session_id,
            "turn": turn,
            "model": model,
            "prompt_tokens": int(prompt_tokens or 0),
            "gen_tokens": int(gen_tokens or 0),
            "latency_ms": int(latency_ms),
            "had_tool_calls": bool(had_tool_calls),
        },
    )
