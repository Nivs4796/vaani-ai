# Vaani AI

Your Gmail and Google Calendar assistant, running on a local model. *Vaani* (वाणी) means voice or speech.

A chat assistant that manages one account's real Gmail and Google Calendar, driven by `qwen2.5:7b` running locally on Ollama. Mail never leaves the laptop except what you send.

**What it can do.** Brief the inbox (Needs a reply / Updates / Promotions) and your day; read and summarise any email; write, reply to and forward email; archive, mark read or unread, star, label, trash and restore mail; create, move, rename, delete and answer calendar events; find free time; remember contacts.

**How writes work.** The model never gets a tool that writes. It can only prepare a card showing exactly what would happen. The write runs in code when you click the card's button (Send, Archive 3, Save change, Delete event...). Every write is read back from Google before it is reported. Gmail uses the `gmail.modify` permission, which cannot permanently delete mail: "delete" always means Trash (kept 30 days). Email recipients and event guests must be a saved contact, an address you typed in the chat, or the sender of the email you are replying to.

**What it cannot do.** Search the web, attach files, change Gmail settings, filters or forwarding, permanently delete anything, or act without being asked (no scheduled briefs yet).

**Trust, earned per workflow.** Replies, scheduling, follow-ups and triage each sit on a ladder: Off, Suggest (a card waits for your click), One-tap (a one-line card with one button) or Auto. Code decides, never the model. Auto stays locked for anything that leaves your account until you switch on "send without asking"; trash is never automatic. Every approved card is logged with whether Google verified it, and after N clean approvals in a row (no edit, no skip, verified) Vaani offers to move that workflow up one level. The Proof panel shows those rows.

**Brief first.** A new chat opens with the inbox brief already built in code (Needs a reply, Follow-ups, Worth knowing, Updates, Promotions) and stamped "built X ago, covers N days". Follow-ups are sent conversations still waiting on the other person; the Nudge button drafts a reply in the same thread, behind the same confirm step.

**Accounts.** With `VAANI_AUTH=google`, the first "Sign in with Google" creates a Vaani account for that Google user (same Gmail and Calendar permissions as below, plus basic profile). Each user has their own memory, Google token (encrypted at rest) and logs; the server takes the user only from a signed session cookie. `VAANI_AUTH=single` (the default) is today's one-user `token.json` mode.

## Prerequisites

- Ollama running (`ollama serve`) with `ollama pull qwen2.5:7b`
- Python 3.12 and a venv at `.venv` (`python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`)
- `credentials.json` (Desktop OAuth client, Gmail API + Calendar API enabled, Testing consent screen with this account as a test user) in the project root; `.env` with `OLLAMA_MODEL=qwen2.5:7b`, `OLLAMA_KEEP_ALIVE=2h`, `OLLAMA_TIMEOUT=120`, `OLLAMA_NUM_CTX=8192`, `OLLAMA_NUM_PREDICT=768`

## Setup and re-auth

```
.venv/bin/python -m assistant.google_auth   # browser consent once; writes token.json (gmail.modify, calendar.events)
.venv/bin/python -c "import assistant.agent, assistant.cli"
```

Testing-mode refresh tokens expire after 7 days. `google_auth.py` catches the `RefreshError`, deletes `token.json` and re-runs consent; run the command above again the morning of the demo rather than discovering it live.

## Run

- REPL: `.venv/bin/python -m assistant.cli` (`/new`, `/cancel`, `/savedraft`, `/quit`; a bare `yes` line carries out the pending card; "yes, but make it 4pm" goes to the model instead). On the web page only the card's button confirms; typing yes there just points at the button.
- Web page: `.venv/bin/uvicorn assistant.web:app --port 8000` then open `http://localhost:8000` (Confirm / Cancel buttons, event link, New chat). If another project is already on 8000, use `--port 8010` and open that instead.
- Demo mode with sign-in: `.venv/bin/python -m assistant.auth init-env` once (adds `VAANI_TOKEN_KEY` and `VAANI_SESSION_SECRET` to `.env`), copy the flags you want from `.env.example` (`VAANI_AUTH=google`, `VAANI_AUTONOMY=1`, `VAANI_PROMOTION=1`, `VAANI_BRIEF_ON_OPEN=1`, `VAANI_FOLLOWUPS=1`, `VAANI_THEME=ledger`), then `.venv/bin/uvicorn assistant.web:app --host 127.0.0.1 --port 8010` and open exactly `http://127.0.0.1:8010/`. Sign-in reuses the Desktop OAuth client in `credentials.json` with a loopback redirect, so no Cloud Console change is needed; the account must be a test user on the consent screen. `python -m assistant.auth import-token` turns today's `token.json` into the first account without a new consent.
- Autonomy state: `python -m assistant.autonomy show` / `reset [--user <email>]` (reset before a demo so test approvals do not count).
- Undo and step-back: a carried-out mailbox change (archive, read, star, trash and their opposites) has an Undo button on its receipt; Undo ends that workflow's streak and steps an Auto workflow down to One-tap. Two skipped cards in a row at One-tap or Auto bring a "step back?" offer. `VAANI_NUDGE_MODEL=1` lets the local model rewrite nudge text; code keeps the template when the rewrite fails its checks.
- All offline suites at once: `python -m evals.all` (writes `data/eval_status.json`, which the Proof panel shows as "Offline checks: N of M passing").
- Evals: `.venv/bin/python -m evals.run` (11 prompt-to-expected-tool cases; reads your real mail and calendar but can never confirm a card)
- Offline checks, no model and no Google: `python -m evals.test_manage` (write actions against a fake Gmail and Calendar), `evals.test_guard`, `evals.test_attendees`, `evals.test_inbox`, `evals.test_auth` (sign-in, sessions, CSRF, user isolation), `evals.test_autonomy` (the ladder), `evals.test_promotion` (streaks and offers), `evals.test_brief`, `evals.test_followups`. They pin every `VAANI_*` flag to its default and log to a temp folder, so `.env` and `logs/` never affect them.
- Smoke test: `.venv/bin/python -m spike.smoke_core` (full Andy flow; creates and deletes one test event)
- Demo smoke test: `.venv/bin/python spike/smoke_demo.py` (brief on open, Reply summary, a nudge and a triage card that are skipped, three drafts saved to your own address to earn a promotion offer, Proof rows; nothing is sent and no mailbox change is made)
- Logs: `logs/tools.jsonl` (one line per tool call), `logs/turns.jsonl` (one line per model call)

## Demo prompts, in order

1. "Check my latest emails and give me updates" (built in code in about 2 s: Needs a reply / Updates / Promotions with real senders and the true unread count)
1b. "What does Ravi want?", using any sender from your brief (code finds and reads the email; the model writes the summary). Then "reply to them saying ..." or "forward it to ..." for a card.
2. "Add a call with Andy tomorrow at 3pm" (two Andys in memory, so it asks which)
3. "Andy Chen" (draft card: 30 minutes, andy.chen@example.test; nothing written yet)
4. Confirm (code runs `commit_pending`, the one write path for every card, which inserts the event, reads it back from Google and shows the verified link; the model is not asked to decide)
5. "Also book Andy Patel for yesterday at 3pm" (declined as past; chat keeps working)
6. "What's on my calendar this week?" (real events plus the new one, Asia/Kolkata)
7. Optional: "Remember that Andy means Andy Chen", `/new`, "Call with Andy Monday 10am", then Cancel. Delete the test event afterwards.

## Runs today / enforced in code / next week

| Runs today | Enforced in code (not by the prompt) | Next week |
|---|---|---|
| Inbox brief (built in code: Gmail categories, unsubscribe and bulk headers, and "Person (Service)" senders sort mail into Needs a reply / Updates / Promotions; one batched Gmail request), email follow-ups ("what does X want?": code reads the email, the model summarises), calendar read, contact resolution, memory, saving contacts (`save_contact`, typed addresses only) | Confirm gate: `commit_pending` (the one write path for every card, new events included) refuses unless the server marked that exact card confirmed; idempotent per card. The Confirm click itself is executed by code (`CONFIRM_MODE=code`, the default): the server runs it and builds the reply from Google's read-back, so the model never decides whether a confirmed write happens. `CONFIRM_MODE=model` asks the model to call the tool instead, kept only for comparison because the 7B model skips the call on long transcripts | Claude Sonnet 5.5 provider behind the same `complete()` with a cost footer |
| Draft, Confirm, Create, re-read from Google | Draft validators: past start, end <= start, duration 5..480, attendee emails; default 30 min; aware datetimes in the memory timezone; "which Andy?" is enforced (a bare name shared by two contacts is refused until the user picks one or a remembered "Andy means Andy Chen" fact resolves it); a named weekday must match the date; an attendee must be a saved contact or an address the user typed in this chat (addresses read from emails are never used on their own; `ALLOWED_ATTENDEE_DOMAINS` in `.env` can allow more domains); "today" and "tomorrow" are checked against the drafted date and corrected; a scheduling request with a first name shared by two contacts gets a fixed "which one?" before the model runs; while a draft is pending, any sentence claiming it was confirmed or created is removed; a confirmation is single-use and only the newest draft can be confirmed; the event id is chosen client-side so a retried insert can never duplicate | Twilio SMS / WhatsApp webhook calling the same `run_turn()` |
| Prompt rules: no claims without a tool call, ask which Andy, never invent a draft_id, refuse past times, state what it cannot do | Capability guard: requests to send, reply, forward or message, to search the web or read news, or to edit, move or delete events get a fixed honest reply before the model runs; an output guard removes any sentence where the model offers to write or send an email (a drafted email is replaced whole). Every tool returns `ERROR: ...; nothing was changed; ...` instead of raising; Google calls retry twice; model outages roll the transcript back | Postgres via the marked swap point in `obs.py` |
| Footer and JSONL observability, 7-case eval, offline checks: `python -m evals.test_guard` (67), `python -m evals.test_attendees` (21) and `python -m evals.test_inbox` (42) | Verified event link comes from the tool result, never from model prose; `sendUpdates="none"` | RAG over mail bodies (embeddinggemma + pgvector), email replies behind the same gate |

## Morning-of checklist

- `ollama serve` is running and `ollama list` shows `qwen2.5:7b`
- Warm the model: `.venv/bin/python spike/ollama_probe.py` or one throwaway REPL turn (cold first turn is 10-15 s)
- Re-auth: `.venv/bin/python -m assistant.google_auth` prints "Connected as ..."
- Seed data: two real events on this week's calendar; if possible one mail from "Andy Chen" asking for a call plus one FYI mail
- `.venv/bin/python -m evals.run` passes 5/5; delete any leftover "Call with Andy" test event
- Laptop plugged in, sleep off, Gmail and Calendar tabs open, two terminals ready
