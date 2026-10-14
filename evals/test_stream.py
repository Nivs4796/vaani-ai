"""Offline checks for POST /chat/stream: live steps while a turn runs, then the result.

No model, no Google, no disk history: flow.chat / confirm / skip are replaced by fakes
that call @logged fake tools, and web._remember by a recorder. Proves the event order
(step start/end pairs with plain labels, then result, then done), that the result is
exactly what POST /chat returns, that the step sink never leaks past a request, and that
obs.logged behaves exactly as before when nobody listens.

Run: .venv/bin/python -m evals.test_stream   (exit 1 on any failure)
"""

from __future__ import annotations

import json
import os
import sys

from fastapi.testclient import TestClient

from assistant import flow, manage, obs, steps, tools, web
from evals import test_manage as tm

RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    if not ok:
        print(f"FAIL  {name}\n      {str(detail)[:400]}")


# --------------------------------------------------------------------------- fakes


@obs.logged
def list_recent_emails(max_results=10, query=""):
    return "2 emails: Ravi Mehta, Paula Sanders"


@obs.logged
def list_calendar_events(days_ahead=7, on_date=""):
    return "1 event: call with Sam Lee"


@obs.logged
def read_email(message_id=""):
    raise RuntimeError("fake Gmail is down")


REMEMBERED: list[tuple] = []
SEEN: dict = {}


async def fake_remember(request, session_id, kind, req, res):
    REMEMBERED.append((session_id, kind, req, res))


def turn_dict(reply: str, tool_calls: list) -> dict:
    return {"reply": reply, "pending_draft": None, "tool_calls": tool_calls,
            "usage": {"prompt_tokens": 0, "gen_tokens": 0, "model_calls": 1}, "latency_ms": 0, "model": "fake",
            "footer": "fake footer", "receipt": None, "offer": None, "decision": None}


def fake_chat(session, text):
    SEEN["lock_held"] = web.SESSIONS[session.id].lock.locked()
    SEEN["sink_in_worker"] = obs.step_sink.get()
    a = list_recent_emails(5, "in:inbox")
    b = list_calendar_events(1)
    return turn_dict(f"Alex, you asked: {text}", [{"name": "list_recent_emails", "ok": obs.result_ok(a)},
                                                  {"name": "list_calendar_events", "ok": obs.result_ok(b)}])


def fake_chat_failing_tool(session, text):
    r = read_email("m1")
    return turn_dict("I could not open that email.", [{"name": "read_email", "ok": obs.result_ok(r)}])


def fake_chat_crash(session, text):
    list_recent_emails()
    raise RuntimeError("model fell over")


def fake_confirm(session, draft_id, variant=None):
    list_calendar_events(1)
    return turn_dict(f"Saved {draft_id} ({variant})", [])


def fake_skip(session, draft_id):
    return turn_dict(f"Skipped {draft_id}", [])


def events_of(r) -> list[dict]:
    """Parse an SSE body: blocks split by a blank line, each one `data: <json>` line."""
    out = []
    for block in r.text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        if not all(line.startswith("data: ") for line in lines):
            raise ValueError(f"not an SSE data block: {block!r}")
        out.append(json.loads("\n".join(line[len("data: "):] for line in lines)))
    return out


# --------------------------------------------------------------------------- checks


def check_labels():
    names = set()
    for mod in (tools, manage):
        for name, obj in vars(mod).items():
            code = getattr(obj, "__code__", None)
            if callable(obj) and getattr(obj, "__wrapped__", None) and code is obs.logged(lambda: 0).__code__:
                names.add(name)
    check("found the @logged tools", len(names) >= 15, sorted(names))
    missing = sorted(n for n in names if steps.label_for(n) == "Working")
    check("every @logged tool has a plain label", not missing, missing)
    check("labels read as plain words", steps.label_for("list_recent_emails") == "Reading your inbox"
          and steps.label_for("list_calendar_events") == "Checking your calendar"
          and steps.done_label_for("list_recent_emails") == "Read your inbox", steps.STEPS)
    check("an unknown tool is just 'Working'", steps.label_for("mystery_tool") == "Working" and steps.label_for(None) == "Working")


def check_logged_unchanged():
    log = tm.Path(tm.tempfile.mkdtemp()) / "tools.jsonl"
    obs.TOOLS_LOG = log
    check("no sink by default", obs.step_sink.get() is None)
    out = list_recent_emails(3)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    check("without a sink a tool returns its own result", out == "2 emails: Ravi Mehta, Paula Sanders", out)
    check("without a sink the log row is unchanged",
          len(rows) == 1 and set(rows[0]) == {"ts", "user_id", "session_id", "turn", "tool", "args", "ms", "ok", "error"}
          and rows[0]["tool"] == "list_recent_emails" and rows[0]["ok"] is True, rows)
    err = read_email("m9")
    check("without a sink a raising tool still becomes an ERROR string", isinstance(err, str) and err.startswith("ERROR:"), err)

    heard = []
    token = obs.step_sink.set(heard.append)
    try:
        list_calendar_events(2)
        read_email("m9")
    finally:
        obs.step_sink.reset(token)
    check("a sink hears start then end for each call",
          [(e["phase"], e["tool"]) for e in heard] == [("start", "list_calendar_events"), ("end", "list_calendar_events"),
                                                       ("start", "read_email"), ("end", "read_email")], heard)
    check("an end event carries ms and ok", heard[1].get("ok") is True and isinstance(heard[1].get("ms"), int)
          and heard[3].get("ok") is False, heard)

    def broken(_ev):
        raise ValueError("listener broke")

    before = len(log.read_text().splitlines())
    token = obs.step_sink.set(broken)
    try:
        out = list_recent_emails(3)
    finally:
        obs.step_sink.reset(token)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    check("a sink that raises never breaks the tool call", out == "2 emails: Ravi Mehta, Paula Sanders", out)
    check("the log row is still written when the sink raises", len(rows) == before + 1 and rows[-1]["tool"] == "list_recent_emails"
          and rows[-1]["ok"] is True, rows[-2:])


def check_stream():
    c = TestClient(web.app)
    sid = c.post("/new").json()["session_id"]

    flow.chat = fake_chat
    REMEMBERED.clear()
    r = c.post("/chat/stream", json={"session_id": sid, "text": "what is new today?", "via": "voice"})
    check("stream answers 200 as an event stream", r.status_code == 200 and r.headers.get("content-type", "").startswith("text/event-stream"),
          (r.status_code, r.headers.get("content-type")))
    check("stream is not cached or buffered", r.headers.get("cache-control") == "no-cache" and r.headers.get("x-accel-buffering") == "no",
          dict(r.headers))
    evs = events_of(r)
    shape = [(e["type"], e.get("phase"), e.get("tool")) for e in evs]
    want = [("step", "start", "list_recent_emails"), ("step", "end", "list_recent_emails"),
            ("step", "start", "list_calendar_events"), ("step", "end", "list_calendar_events"),
            ("result", None, None), ("done", None, None)]
    check("events: start/end per tool, then result, then done", shape == want, shape)
    stepevs = [e for e in evs if e["type"] == "step"]
    check("steps carry plain labels", [e["label"] for e in stepevs] == ["Reading your inbox"] * 2 + ["Checking your calendar"] * 2,
          [e["label"] for e in stepevs])
    check("step events have the contract's fields", all({"type", "label", "tool", "phase", "ms", "ok"} <= set(e) for e in stepevs), stepevs)
    check("an end step has ms, ok and the done label", all(isinstance(e["ms"], int) and e["ok"] is True for e in stepevs if e["phase"] == "end")
          and stepevs[1].get("done") == "Read your inbox", stepevs)
    check("the session lock is held while the turn runs", SEEN.get("lock_held") is True, SEEN)
    result = evs[-2].get("result") if len(evs) >= 2 else None
    reply = "Alex, you asked: what is new today?"
    check("the reply text arrives only inside the result", sum(reply in json.dumps(e) for e in evs) == 1 and (result or {}).get("reply") == reply, evs)
    check("history recorded like POST /chat (kind chat, text and via)",
          REMEMBERED == [(sid, "chat", {"text": "what is new today?", "via": "voice"}, result)], REMEMBERED)

    REMEMBERED.clear()
    plain = c.post("/chat", json={"session_id": sid, "text": "what is new today?", "via": "voice"})
    check("result equals what POST /chat returns", plain.status_code == 200 and plain.json() == result, (plain.json(), result))
    check("POST /chat records the same history row", REMEMBERED == [(sid, "chat", {"text": "what is new today?", "via": "voice"}, plain.json())],
          REMEMBERED)

    check("the sink is reset after the request (test thread)", obs.step_sink.get() is None)
    leaks = []
    for _ in range(6):  # the threadpool reuses workers: none of them may still hold the stream's sink
        c.post("/chat", json={"session_id": sid, "text": "again"})
        leaks.append(SEEN.get("sink_in_worker"))
    check("the sink never leaks into later turns", leaks == [None] * 6, leaks)
    r = c.post("/chat/stream", json={"session_id": sid, "text": "typed one"})
    check("via defaults to typed in history", REMEMBERED[-1][1:3] == ("chat", {"text": "typed one", "via": "typed"}), REMEMBERED[-1:])

    flow.chat = fake_chat_failing_tool
    evs = events_of(c.post("/chat/stream", json={"session_id": sid, "text": "open the email from Ravi Mehta"}))
    shape = [(e["type"], e.get("phase"), e.get("ok")) for e in evs]
    check("a failing tool still gives start, end ok=false, result, done",
          shape == [("step", "start", None), ("step", "end", False), ("result", None, None), ("done", None, None)], shape)
    check("the failing step is labelled", evs[0].get("label") == "Opening the email", evs[0])

    flow.chat = fake_chat_crash
    REMEMBERED.clear()
    r = c.post("/chat/stream", json={"session_id": sid, "text": "crash please"})
    evs = events_of(r)
    plain = c.post("/chat", json={"session_id": sid, "text": "crash please"}).json()
    check("a crashing turn ends with the backstop result and done", [e["type"] for e in evs] == ["step", "step", "result", "done"]
          and "Something went wrong on my side (RuntimeError)" in evs[2]["result"]["reply"], evs)
    check("the backstop result equals POST /chat's", evs[2]["result"] == plain, (evs[2].get("result"), plain))
    check("a crashed turn is not recorded (as POST /chat)", REMEMBERED == [], REMEMBERED)

    flow.confirm = fake_confirm
    REMEMBERED.clear()
    evs = events_of(c.post("/chat/stream", json={"session_id": sid, "confirm_draft_id": "d7", "confirm_variant": "short"}))
    check("confirm goes through flow.confirm with draft id and variant",
          [e["type"] for e in evs] == ["step", "step", "result", "done"] and evs[2]["result"]["reply"] == "Saved d7 (short)", evs)
    check("confirm is recorded as kind confirm", REMEMBERED and REMEMBERED[0][1:3] == ("confirm", {"draft_id": "d7", "variant": "short"}), REMEMBERED)

    flow.skip = fake_skip
    REMEMBERED.clear()
    evs = events_of(c.post("/chat/stream", json={"session_id": sid, "cancel_draft_id": "d8", "text": "ignored"}))
    check("cancel goes through flow.skip (and wins over text)", [e["type"] for e in evs] == ["result", "done"]
          and evs[0]["result"]["reply"] == "Skipped d8", evs)
    check("cancel is recorded as kind cancel", REMEMBERED and REMEMBERED[0][1:3] == ("cancel", {"draft_id": "d8"}), REMEMBERED)

    r = c.post("/chat/stream", json={"session_id": "nope1234", "text": "hi"})
    p = c.post("/chat", json={"session_id": "nope1234", "text": "hi"})
    check("an unknown session is the same JSON 404 as POST /chat", r.status_code == 404 and r.json() == p.json()
          and "unknown session_id" in r.json().get("error", ""), (r.status_code, r.text))
    r = c.post("/chat/stream", json={"text": "no session"})
    check("a body without session_id is a JSON 422", r.status_code == 422 and r.json().get("error", "").startswith("bad request:"),
          (r.status_code, r.text))
    r = c.post("/chat/stream", content="not json", headers={"Content-Type": "application/json"})
    check("a body that is not JSON is a 4xx, never a 500", 400 <= r.status_code < 500, (r.status_code, r.text))
    check("no turn task is left running", not __import__("assistant.routes_stream", fromlist=["_RUNNING"])._RUNNING)

    web.SESSIONS[sid].owner = "someone-else"  # another user's session must look unknown
    try:
        r = c.post("/chat/stream", json={"session_id": sid, "text": "hi"})
    finally:
        web.SESSIONS[sid].owner = None
    check("another user's session is a 404 (owner check)", r.status_code == 404 and "unknown session_id" in r.text, (r.status_code, r.text))
    os.environ["VAANI_AUTH"] = "google"
    try:
        r = TestClient(web.app).post("/chat/stream", json={"session_id": sid, "text": "hi"})
    finally:
        os.environ["VAANI_AUTH"] = "single"
    check("google mode: the stream sits behind sign-in (401)", r.status_code == 401 and r.json().get("error") == "sign in required",
          (r.status_code, r.text))


def main() -> int:
    import logging

    for name in ("assistant.web", "assistant.routes_stream", "httpx", "httpx2"):  # the crash checks log on purpose
        logging.getLogger(name).setLevel(logging.CRITICAL)
    tm.setup()  # temp memory file, fake Google services
    web._remember = fake_remember
    real = (flow.chat, flow.confirm, flow.skip)
    for fn in (check_labels, check_logged_unchanged, check_stream):
        try:
            fn()
        except Exception as e:  # a crash in one group is a failure, not the end of the run
            import traceback
            check(f"{fn.__name__} crashed", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")
    flow.chat, flow.confirm, flow.skip = real
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} stream checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
