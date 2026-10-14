"""Offline checks for chat history (assistant/history.py) and its routes (assistant/routes_history.py).

No network, no model, no Google: data/vaani.db, data/memory.json and logs/feedback.jsonl are
replaced by a temp directory, and agent.run_turn by a fake. Proves: turns are recorded and read
back in order, titles are code-built (first typed text, word boundary, at most 48 chars), chats
that were only opened stay hidden, Today / Yesterday / Earlier follow the user's time zone, two
users never see each other's chats, rename and soft delete work, blobs hold no plaintext when
VAANI_TOKEN_KEY is set (and plain JSON never crashes without one), and the routes answer in
single mode with the contract's shapes.

Run: .venv/bin/python -m evals.test_history   (exit 1 on any failure)
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import sys
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

os.environ["VAANI_TOKEN_KEY"] = ""  # set but empty: python-dotenv will not fill it from .env
warnings.filterwarnings("ignore", message=".*httpx.*")

from fastapi.testclient import TestClient  # noqa: E402

from assistant import agent, google_auth, history, llm, memory, store, web  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
BASE = "http://127.0.0.1:8031"
A, B = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


@dataclass
class FakeTurn:
    reply: str
    pending_draft: dict | None = None


def fake_run_turn(session, text=None, **kw):
    return FakeTurn(reply=f"Echo: {text}")


def setup() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="vaani-history-"))
    store.DB_FILE = tmp / "vaani.db"
    memory.MEMORY_FILE = tmp / "memory.json"
    memory.USERS_DIR = tmp / "users"
    single = copy.deepcopy(memory.DEFAULT)
    single.update(name="Alex", timezone="Asia/Kolkata")
    memory.MEMORY_FILE.write_text(json.dumps(single))
    history.FEEDBACK_LOG = tmp / "logs" / "feedback.jsonl"
    web.log.setLevel(logging.ERROR)
    llm.check_server = lambda: {"model_present": True, "version": "test", "model": llm.MODEL}
    google_auth.connected_email = lambda user_id=None: "alex@example.org"
    agent.run_turn = fake_run_turn
    return tmp


def blobs(chat_id: str) -> list[bytes]:
    with sqlite3.connect(store.DB_FILE) as conn:
        return [bytes(r[0]) for r in conn.execute("SELECT blob FROM chat_items WHERE chat_id = ? ORDER BY seq", (chat_id,))]


def set_updated(user_key: str, chat_id: str, iso: str):
    with sqlite3.connect(store.DB_FILE) as conn:
        conn.execute("UPDATE chats SET updated_at = ? WHERE user_key = ? AND id = ?", (iso, user_key, chat_id))


def ids(listing: dict) -> list[str]:
    return [c["id"] for g in listing["groups"] for c in g["chats"]]


def check_titles():
    check("title: short text kept", history.make_title("  What's on my   calendar today? ") == "What's on my calendar today?")
    long = "Please draft a polite reply to Ravi Mehta about moving the quarterly review to Thursday"
    t = history.make_title(long)
    check("title: at most 48 chars", len(t) <= 48, t)
    check("title: cut on a word boundary", t.endswith("…") and long.startswith(t[:-1]) and long[len(t) - 1] == " ", t)
    one = history.make_title("x" * 90)
    check("title: one long word is hard-cut", len(one) == 48 and one.endswith("…"), one)
    check("title: empty stays empty", history.make_title("   ") == "")


def check_record_and_read():
    history.record(None, "s1", "new", {}, {"session_id": "s1", "brief": None})
    check("hidden: an opened chat is not listed", "s1" not in ids(history.list_chats(None)))
    history.record(None, "s1", "chat", {"text": "Find a free hour tomorrow afternoon", "via": "typed"}, {"reply": "Two slots are free."})
    history.record(None, "s1", "chat", {"text": "Book the first one", "via": "voice"}, {"reply": "Drafted.", "pending_draft": {"draft_id": "d1"}})
    history.record(None, "s1", "confirm", {"draft_id": "d1", "variant": None}, {"reply": "Created.", "receipt": {"status": "CREATED"}})
    listing = history.list_chats(None, live={"s1"})
    row = next((c for g in listing["groups"] for c in g["chats"] if c["id"] == "s1"), None)
    check("list: chat shows once it has a turn", row is not None, listing)
    check("list: title is the first typed text", row and row["title"] == "Find a free hour tomorrow afternoon", row)
    check("list: live flag", row and row["live"] is True, row)
    check("list: group is Today", listing["groups"] and listing["groups"][0]["label"] == "Today", listing)
    chat = history.get_chat(None, "s1")
    kinds = [i["kind"] for i in chat["items"]]
    check("get: items in order", kinds == ["new", "chat", "chat", "confirm"], kinds)
    check("get: request and response read back", chat["items"][2]["request"] == {"text": "Book the first one", "via": "voice"}
          and chat["items"][3]["response"]["receipt"]["status"] == "CREATED", chat["items"])
    check("get: every item has a ts", all(i["ts"] for i in chat["items"]))
    check("get: not live unless told", chat["live"] is False)
    check("plain: no key stores plain JSON", blobs("s1")[1].startswith(b"{"), blobs("s1")[1][:20])
    history.record(None, "s2", "action", {"action": "archive", "message_id": "m1"}, {"reply": "Archived."})
    check("action-only chat is listed with a fallback title",
          any(c["id"] == "s2" and c["title"] == history.UNTITLED for g in history.list_chats(None)["groups"] for c in g["chats"]))
    history.record(None, "s2", "chat", {"text": "And the next one?"}, {"reply": "Done."})
    check("first chat text names an action-first chat", history.get_chat(None, "s2")["title"] == "And the next one?")
    history.record(None, "s1", "chat", {"text": "Something else entirely"}, {"reply": "ok"})
    check("later text never retitles", history.get_chat(None, "s1")["title"] == "Find a free hour tomorrow afternoon")
    check("newest first", ids(history.list_chats(None))[0] == "s1", ids(history.list_chats(None)))
    history.record(None, "", "chat", {"text": "no session"}, {"reply": "x"})
    history.record(None, "s9", "chat", "not a dict", ["not", "a", "dict"])
    check("odd input never crashes and stores empty dicts", history.get_chat(None, "s9")["items"][0]["request"] == {})


def check_users_rename_delete():
    history.record(A, "sa", "chat", {"text": "Reply to Paula Sanders"}, {"reply": "Drafted."})
    history.record(B, "sb", "chat", {"text": "Email Sam Lee about lunch"}, {"reply": "Drafted."})
    history.record(B, "sa", "chat", {"text": "Same id, other user"}, {"reply": "ok"})
    check("isolation: A sees only A", ids(history.list_chats(A)) == ["sa"], ids(history.list_chats(A)))
    check("isolation: B sees only B", sorted(ids(history.list_chats(B))) == ["sa", "sb"])
    check("isolation: same id, separate chats", history.get_chat(A, "sa")["title"] == "Reply to Paula Sanders"
          and history.get_chat(B, "sa")["title"] == "Same id, other user")
    check("isolation: A cannot read B's chat", history.get_chat(A, "sb") is None)
    check("isolation: single mode sees no user's chats", "sb" not in ids(history.list_chats(None)))
    check("isolation: A cannot rename B's chat", history.rename(A, "sb", "Mine now") is None)
    check("isolation: A cannot delete B's chat", history.delete(A, "sb") is False)
    out = history.rename(B, "sb", "   Lunch   with\nSam  ")
    check("rename: spaces collapsed", out == {"id": "sb", "title": "Lunch with Sam"}, out)
    check("rename: read back", history.get_chat(B, "sb")["title"] == "Lunch with Sam")
    check("rename: capped at 80", len(history.rename(B, "sb", "y" * 200)["title"]) == 80)
    try:
        history.rename(B, "sb", "  ")
        check("rename: empty refused", False)
    except ValueError:
        check("rename: empty refused", True)
    history.record(B, "sb", "chat", {"text": "after rename"}, {"reply": "ok"})
    check("rename survives a later turn", history.get_chat(B, "sb")["title"] == "y" * 80)
    check("delete: soft delete", history.delete(B, "sb") is True)
    check("delete: gone from list and get", "sb" not in ids(history.list_chats(B)) and history.get_chat(B, "sb") is None)
    check("delete: twice is False", history.delete(B, "sb") is False)
    with sqlite3.connect(store.DB_FILE) as conn:
        kept = conn.execute("SELECT deleted FROM chats WHERE user_key = ? AND id = 'sb'", (B,)).fetchone()
    check("delete: row kept with deleted = 1", kept == (1,), kept)


def check_grouping():
    tz = timezone(timedelta(hours=5, minutes=30))  # memory.json says Asia/Kolkata
    now = datetime(2026, 10, 14, 1, 0, tzinfo=tz)  # 01:00 local = 19:30 UTC the day before
    for cid in ("g1", "g2", "g3", "g4"):
        history.record(None, cid, "chat", {"text": f"Grouping {cid}"}, {"reply": "ok"})
    set_updated("single", "g1", "2026-10-13T19:45:00+00:00")  # 01:15 local, today
    set_updated("single", "g2", "2026-10-13T18:00:00+00:00")  # 23:30 local, yesterday (UTC says today)
    set_updated("single", "g3", "2026-10-12T20:00:00+00:00")  # 01:30 local on the 13th, yesterday
    set_updated("single", "g4", "2026-10-11T10:00:00+00:00")  # earlier
    groups = {g["label"]: [c["id"] for c in g["chats"] if c["id"].startswith("g")] for g in history.list_chats(None, now=now)["groups"]}
    check("group: Today in the user's zone", groups.get("Today") == ["g1"], groups)
    check("group: Yesterday in the user's zone", groups.get("Yesterday") == ["g2", "g3"], groups)
    check("group: Earlier", groups.get("Earlier", [])[:1] == ["g4"], groups)
    labels = [g["label"] for g in history.list_chats(None, now=now)["groups"]]
    check("group: order Today, Yesterday, Earlier", labels == [x for x in ("Today", "Yesterday", "Earlier") if x in labels], labels)
    for i in range(60):
        history.record("33333333-cccc-4ccc-8ccc-cccccccccccc", f"m{i}", "chat", {"text": f"chat {i}"}, {"reply": "ok"})
    many = ids(history.list_chats("33333333-cccc-4ccc-8ccc-cccccccccccc"))
    check("list: at most 50", len(many) == 50 and many[0] == "m59", (len(many), many[:2]))


def check_encryption():
    os.environ["VAANI_TOKEN_KEY"] = Fernet.generate_key().decode()
    secret = "Paula Sanders asked about the budget"
    history.record(A, "enc", "chat", {"text": secret}, {"reply": f"Reply mentions {secret}"})
    raw = blobs("enc")[0]
    check("encrypted: blob has no plaintext", b"Paula" not in raw and b"budget" not in raw and not raw.startswith(b"{"), raw[:30])
    check("encrypted: reads back", history.get_chat(A, "enc")["items"][0]["request"]["text"] == secret)
    check("encrypted: plain rows from before the key still read", history.get_chat(None, "s1")["items"][1]["response"]["reply"] == "Two slots are free.")
    os.environ["VAANI_TOKEN_KEY"] = Fernet.generate_key().decode()  # a different key: never crash
    item = history.get_chat(A, "enc")["items"][0]
    check("wrong key: item flagged, no crash", item.get("unreadable") is True and item["request"] == {}, item)
    os.environ["VAANI_TOKEN_KEY"] = ""


def check_feedback():
    history.feedback(A, "s1", "up", "Two slots are free. " * 30)
    history.feedback(None, "s1", "down", "short")
    lines = [json.loads(x) for x in history.FEEDBACK_LOG.read_text().splitlines()]
    check("feedback: two lines", len(lines) == 2, lines)
    check("feedback: excerpt capped at 200", len(lines[0]["excerpt"]) <= 200 and lines[0]["value"] == "up" and lines[0]["user"] == A, lines[0])
    check("feedback: single user key", lines[1]["user"] == "single" and lines[1]["value"] == "down")
    try:
        history.feedback(A, "s1", "meh", "")
        check("feedback: bad value refused", False)
    except ValueError:
        check("feedback: bad value refused", True)


def check_routes():
    os.environ["VAANI_AUTH"] = "single"
    c = TestClient(web.app, base_url=BASE)
    sid = c.post("/new").json().get("session_id")
    check("route: /new opens a session", bool(sid))
    check("route: an opened chat is hidden", sid not in ids(c.get("/chats").json()))
    r = c.post("/chat", json={"session_id": sid, "text": "Check my latest emails and give me updates", "via": "voice"})
    check("route: /chat runs", r.status_code == 200 and r.json().get("reply", "").startswith("Echo:"), r.text)
    r = c.get("/chats")
    body = r.json()
    row = next((x for g in body.get("groups", []) for x in g["chats"] if x["id"] == sid), None)
    check("route: GET /chats shape", r.status_code == 200 and row is not None and set(row) == {"id", "title", "updated_at", "live"}, body)
    check("route: recorded by web._remember, title from text, live", row and row["title"] == "Check my latest emails and give me updates" and row["live"] is True, row)
    r = c.get(f"/chats/{sid}")
    one = r.json()
    check("route: GET /chats/{id} shape", r.status_code == 200 and set(one) == {"id", "title", "live", "items"} and one["live"] is True, one)
    check("route: items carry kind, request, response, ts", [i["kind"] for i in one["items"]] == ["new", "chat"]
          and one["items"][1]["request"] == {"text": "Check my latest emails and give me updates", "via": "voice"}
          and set(one["items"][1]) >= {"kind", "request", "response", "ts"}, one["items"])
    old = c.get("/chats/s1").json()
    check("route: an earlier chat is not live", old.get("live") is False and old.get("title"), old)
    r = c.patch(f"/chats/{sid}", json={"title": "Inbox catch-up"})
    check("route: PATCH renames", r.status_code == 200 and r.json() == {"ok": True, "id": sid, "title": "Inbox catch-up"}, r.text)
    r = c.patch(f"/chats/{sid}", json={"title": " "})
    check("route: PATCH empty title is 400", r.status_code == 400 and r.json().get("error"), r.text)
    r = c.get("/chats/sa")  # user A's chat; single mode is a different user
    check("route: another user's chat is 404 JSON", r.status_code == 404 and "error" in r.json(), r.text)
    check("route: PATCH unknown is 404", c.patch("/chats/nope", json={"title": "x"}).status_code == 404)
    r = c.delete(f"/chats/{sid}")
    check("route: DELETE", r.status_code == 200 and r.json() == {"ok": True}, r.text)
    check("route: deleted chat leaves the list", sid not in ids(c.get("/chats").json()))
    check("route: DELETE again is 404", c.delete(f"/chats/{sid}").status_code == 404)
    r = c.post("/feedback", json={"session_id": sid, "value": "up", "excerpt": "Echo"})
    check("route: POST /feedback", r.status_code == 200 and r.json() == {"ok": True}, r.text)
    last = json.loads(history.FEEDBACK_LOG.read_text().splitlines()[-1])
    check("route: feedback line written", last["session_id"] == sid and last["value"] == "up" and last["excerpt"] == "Echo", last)
    r = c.post("/feedback", json={"session_id": sid, "value": "sideways"})
    check("route: bad feedback value is 400", r.status_code == 400, r.text)


def main() -> int:
    setup()
    for fn in (check_titles, check_record_and_read, check_users_rename_delete, check_grouping, check_encryption,
               check_feedback, check_routes):
        try:
            fn()
        except Exception as e:  # a crash is a failed check, not a stack trace that hides the rest
            check(f"{fn.__name__} ran", False, f"{type(e).__name__}: {e}")
    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ->  {detail[:300]}"))
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} history checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
