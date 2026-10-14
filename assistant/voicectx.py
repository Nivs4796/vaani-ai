"""Shorter model context in voice mode (PARSER). agent._Loop.run calls trim() when
obs.current_mode is "voice"; it returns a NEW list and never changes session.messages.

What the model sees in voice mode: the system message (a copy, with one style line
added), then only the last VAANI_VOICE_CTX_TURNS user turns (default 3). The cut is
always at a user message, so every assistant tool call keeps its tool results; tool
results older than the latest user turn are shortened to about 800 characters. A tool
message without its call, or a call without results, never reaches Ollama.
Contract: .build/CONTRACT-v3.md.
"""

from __future__ import annotations

import os

STYLE = "Voice mode: answer in one or two short spoken sentences; no lists, links or markdown."
DEFAULT_TURNS = 3
OLD_TOOL_MAX = 800
_SYNTHETIC_USER = ("User clicked", "You did not call", "You wrote the email in the chat")  # as agent._SYNTHETIC_USER


def _turns() -> int:
    try:
        return max(1, int(os.environ.get("VAANI_VOICE_CTX_TURNS", str(DEFAULT_TURNS)).strip()))
    except ValueError:
        return DEFAULT_TURNS


def _is_turn_start(m: dict) -> bool:
    """A user message the person said or typed (Confirm clicks and code nudges are not new turns)."""
    if m.get("role") != "user":
        return False
    try:
        from assistant import agent

        return not agent._is_synthetic(m)
    except Exception:
        content = str(m.get("content") or "")
        if content.startswith("[now:"):
            content = content.split("]", 1)[-1].lstrip()
        return not content.startswith(_SYNTHETIC_USER)


def _shorten(content: str, limit: int = OLD_TOOL_MAX) -> str:
    text = str(content or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ...[shortened in voice mode]"


def _well_formed(msgs: list[dict]) -> list[dict]:
    """Drop tool messages that do not follow their call, and calls left without any result."""
    out: list[dict] = []
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            prev = out[-1] if out else None
            if prev is None or not (prev.get("role") == "tool" or (prev.get("role") == "assistant" and prev.get("tool_calls"))):
                continue
        out.append(m)
    fixed: list[dict] = []
    for i, m in enumerate(out):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = out[i + 1] if i + 1 < len(out) else None
            if nxt is None or nxt.get("role") != "tool":
                if not str(m.get("content") or "").strip():
                    continue
                m = {k: v for k, v in m.items() if k != "tool_calls"}
        fixed.append(m)
    return fixed


def trim(messages: list[dict]) -> list[dict]:
    msgs = list(messages or [])
    if not msgs:
        return []
    if msgs[0].get("role") == "system":
        system = dict(msgs[0])
        body = msgs[1:]
    else:
        system = {"role": "system", "content": ""}
        body = msgs
    content = str(system.get("content") or "")
    system["content"] = f"{content}\n{STYLE}" if content else STYLE

    starts = [i for i, m in enumerate(body) if _is_turn_start(m)]
    keep_from = starts[-_turns()] if len(starts) >= _turns() else (starts[0] if starts else 0)
    latest = starts[-1] if starts else len(body)
    kept: list[dict] = []
    for i, m in enumerate(body[keep_from:], start=keep_from):
        m = dict(m)  # a copy: session.messages is never changed
        if m.get("role") == "tool" and i < latest:
            m["content"] = _shorten(m.get("content"))
        kept.append(m)
    return [system] + _well_formed(kept)
