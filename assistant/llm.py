"""Provider function over Ollama. Swap point for the Anthropic provider later:
complete(messages, tools) is the only thing agent.py calls.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import ollama
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "2h")
HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT", "300"))
# num_ctx: Ollama defaults to 4096 and silently drops the oldest context (including the
# system prompt) past that; the confirm turn sits near 3k after an inbox brief.
# num_predict: hard cap so a degenerate loop cannot run for the whole timeout.
OPTIONS = {
    "temperature": 0,
    "num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
    "num_predict": int(os.environ.get("OLLAMA_NUM_PREDICT", "768")),
}


class LLMError(Exception):
    """The model server is unreachable, the model is missing, or the call failed."""


_client: ollama.Client | None = None


def _get_client() -> ollama.Client:
    global _client
    if _client is None:
        _client = ollama.Client(host=HOST, timeout=TIMEOUT_S)
    return _client


def _model_present(names: list[str]) -> bool:
    wanted = MODEL if ":" in MODEL else f"{MODEL}:latest"
    return wanted in names or MODEL in names


def check_server() -> dict:
    """{"version", "model", "model_present"}; raises LLMError if unreachable."""
    try:
        with urllib.request.urlopen(f"{HOST}/api/version", timeout=5) as r:
            version = json.load(r).get("version", "?")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMError(f"Ollama is not reachable at {HOST} ({e}). Start it with: ollama serve") from e
    try:
        names = [m.model for m in _get_client().list().models]
    except Exception as e:  # server up but /api/tags failed
        raise LLMError(f"Ollama answered /api/version but listing models failed: {e}") from e
    return {"version": version, "model": MODEL, "model_present": _model_present(names)}


def complete(messages: list[dict], tools: list) -> ollama.ChatResponse:
    """One non-streaming chat call with tools, temperature 0, keep_alive 2h.
    Connection and server errors are wrapped in LLMError."""
    try:
        return _get_client().chat(
            model=MODEL,
            messages=messages,
            tools=tools,
            stream=False,
            options=OPTIONS,
            keep_alive=KEEP_ALIVE,
        )
    except ollama.ResponseError as e:
        detail = getattr(e, "error", None) or str(e)
        if getattr(e, "status_code", None) == 404:
            raise LLMError(f"Model '{MODEL}' is not available on Ollama ({detail}). Run: ollama pull {MODEL}") from e
        raise LLMError(f"Ollama returned an error: {detail}") from e
    except (ConnectionError, ollama.RequestError, httpx.HTTPError, OSError, TimeoutError) as e:
        raise LLMError(f"Ollama at {HOST} is not reachable ({type(e).__name__}). Start it with: ollama serve") from e
