"""Terminal REPL: the demo fallback. Prints the reply and the footer after every turn.

Commands: /new (fresh session), /cancel (drop the pending card), /savedraft (save an email card to Gmail drafts), /quit.
A bare yes/confirm/go ahead/ok while a draft is pending confirms it.

`--user <email>` runs as that signed-in Vaani account (its memory and stored Google
token, see auth.py); without it everything uses token.json and data/memory.json.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from assistant import agent, context, google_auth, llm, memory


def header() -> str:
    data = memory.load()
    tz = data.get("timezone") or "UTC"
    try:
        who = google_auth.connected_email()
    except Exception as e:
        who = f"(Google not connected: {type(e).__name__})"
    scopes = ", ".join(s.rsplit("/", 1)[-1] for s in google_auth.SCOPES)
    try:
        now = datetime.now(ZoneInfo(tz)).strftime("%a %d %b %H:%M %Z")
    except Exception:
        now = datetime.now().strftime("%a %d %b %H:%M")
    return f"Vaani AI · connected as {who} · {scopes} · model: {llm.MODEL} (local) · now: {now}"


def print_result(r: agent.TurnResult) -> None:
    print(f"\nvaani> {r.reply}")
    print(f"  [{r.footer}]")
    if r.pending_draft:
        d = r.pending_draft
        card = d.get("card") or {}
        print(f"  ┌ {card.get('title', 'Draft')} [{d['draft_id']}]")
        for line in card.get("lines") or []:
            print(f"  │ {line}")
        if card.get("body"):
            for line in str(card["body"]).splitlines():
                print(f"  │   {line}")
        if card.get("note"):
            print(f"  │ {card['note']}")
        extra = " or /savedraft" if card.get("alt") else ""
        print(f"  └ Type yes to {card.get('confirm_label', 'Confirm').lower()}{extra}, or /cancel.")


def _sign_in_as(email: str) -> str | None:
    """Set context.current_user to the account with this email. Returns an error message or None."""
    from assistant import auth, store  # only --user touches the account database

    try:
        user = store.get_user_by_email(email)
    except Exception as e:
        return f"could not read the account database ({type(e).__name__}: {e})"
    if not user:
        return f"no Vaani account for {email}; sign in once on the web page (VAANI_AUTH=google) or run `python -m assistant.auth import-token`"
    context.current_user.set(auth.public(user))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m assistant.cli", description="Vaani AI terminal REPL")
    parser.add_argument("--user", metavar="EMAIL", help="run as this signed-in Vaani account instead of token.json")
    args = parser.parse_args(argv)
    if args.user:
        problem = _sign_in_as(args.user)
        if problem:
            print(f"ERROR: {problem}")
            return 2
    try:
        info = llm.check_server()
        if not info["model_present"]:
            print(f"Warning: model {info['model']} is not in `ollama list`; run: ollama pull {info['model']}")
    except llm.LLMError as e:
        print(f"Warning: {e}")
    print(header())
    print("Commands: /new  /cancel  /savedraft  /quit   (Ctrl-C exits)")
    session = agent.new_session()
    while True:
        try:
            line = input("\nyou> ")
        except (KeyboardInterrupt, EOFError):
            print("\nbye")
            return 0
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            print("bye")
            return 0
        if line == "/new":
            session = agent.new_session()
            print("New chat started.")
            continue
        created_before = {d for d, v in session.drafts.items() if v.get("event_id")}
        try:
            if line == "/cancel":
                if not session.pending_draft_id:
                    print("There is no pending draft.")
                    continue
                r = agent.run_turn(session, cancel_draft_id=session.pending_draft_id)
            elif line == "/savedraft" and session.pending_draft_id:
                r = agent.run_turn(session, confirm_draft_id=session.pending_draft_id, confirm_variant="save_draft")
            elif session.pending_draft_id and agent.is_confirmation(line):
                # Only a bare yes/ok/confirm line confirms; "yes, but 4pm" goes to the model.
                r = agent.run_turn(session, confirm_draft_id=session.pending_draft_id)
            else:
                r = agent.run_turn(session, line)
        except KeyboardInterrupt:
            # run_turn already rolled the transcript back (or kept it consistent if a write happened).
            created = [v for d, v in session.drafts.items() if v.get("event_id") and d not in created_before]
            if created:
                for d in created:
                    link = (d.get("created") or {}).get("htmlLink") or ""
                    print(f"\n(interrupted, but the event WAS created: {d.get('summary')} {d.get('start')} {link})".rstrip())
            else:
                print("\n(interrupted; that turn was dropped, nothing was changed)")
            continue
        print_result(r)


if __name__ == "__main__":
    sys.exit(main())
