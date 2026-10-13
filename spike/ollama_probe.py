import json, time, urllib.request, sys

MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:7b"
URL = "http://localhost:11434/api/chat"

TOOLS = [
 {"type":"function","function":{"name":"list_recent_emails","description":"Newest inbox messages as compact rows {id, from, subject, date, unread, snippet}. Use this before summarising the inbox.","parameters":{"type":"object","properties":{"max_results":{"type":"integer","default":10},"query":{"type":"string","default":"in:inbox newer_than:3d"}}}}},
 {"type":"function","function":{"name":"read_email","description":"Decoded body of one message when the snippet is not enough.","parameters":{"type":"object","properties":{"message_id":{"type":"string"}},"required":["message_id"]}}},
 {"type":"function","function":{"name":"list_calendar_events","description":"Primary-calendar events in the next N days.","parameters":{"type":"object","properties":{"days_ahead":{"type":"integer","default":7}}}}},
 {"type":"function","function":{"name":"find_contact","description":"Candidate email addresses for a first name. If 0 or 2+ candidates come back, ask the user which one before drafting.","parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}},
 {"type":"function","function":{"name":"draft_calendar_event","description":"Validate and store a proposed event under a draft_id and return it for the user to confirm. Never writes to the calendar.","parameters":{"type":"object","properties":{"title":{"type":"string"},"start_iso":{"type":"string","description":"ISO 8601 local start, e.g. 2026-10-14T15:00"},"duration_minutes":{"type":"integer","default":30},"attendee_emails":{"type":"array","items":{"type":"string"}},"description":{"type":"string"}},"required":["title","start_iso"]}}},
 {"type":"function","function":{"name":"create_calendar_event","description":"Create a draft on the real calendar. Only call this after the user has explicitly confirmed the draft. Returns what Google stored.","parameters":{"type":"object","properties":{"draft_id":{"type":"string"}},"required":["draft_id"]}}},
 {"type":"function","function":{"name":"remember","description":"Persist a durable fact about the user.","parameters":{"type":"object","properties":{"fact":{"type":"string"}},"required":["fact"]}}},
]

SYSTEM = """You are a personal assistant for Alex. Timezone: Asia/Kolkata.
Known contacts: Andy Chen <andy.chen@example.test>, Andy Patel <andy.patel@example.test>.
Rules:
- Never claim to have read email or the calendar without calling a tool.
- To schedule something: resolve the person first. If a name matches two or more contacts, ask one short question which one; do not guess.
- Then call draft_calendar_event and show the draft. Do NOT call create_calendar_event until the user explicitly confirms (yes / confirm / go ahead).
- Keep replies short and plain."""

def chat(messages, label):
    body = {"model": MODEL, "messages": [{"role":"system","content":SYSTEM}] + messages, "tools": TOOLS, "stream": False, "options": {"temperature": 0}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.load(r)
    wall = time.time() - t0
    msg = out["message"]
    calls = msg.get("tool_calls") or []
    print(f"\n=== {label} ===")
    print(f"wall {wall:.1f}s | prompt_eval {out.get('prompt_eval_count')} tok | gen {out.get('eval_count')} tok | load {out.get('load_duration',0)/1e9:.1f}s")
    for c in calls:
        print("TOOL_CALL:", c["function"]["name"], json.dumps(c["function"].get("arguments")))
    if msg.get("content","").strip():
        print("TEXT:", msg["content"].strip()[:400])
    if not calls and not msg.get("content","").strip():
        print("(empty response)")
    return msg

if __name__ == "__main__":
    # warm-up to load the model so later timings reflect steady state
    chat([{"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] hi"}], "warm-up")

    # 1. inbox brief
    m1 = chat([{"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] Check my latest emails and give me updates"}], "1. inbox brief -> expect list_recent_emails")

    # 1b. feed a tool result and see if it summarises or hallucinates
    if m1.get("tool_calls"):
        rows = json.dumps([
            {"id":"m1","from":"Andy Chen <andy.chen@example.test>","subject":"Call Thursday?","date":"2026-10-13 11:02","unread":True,"snippet":"Can we do a 30 min call Thursday 3pm to go over the proposal?"},
            {"id":"m2","from":"HDFC Bank <alerts@hdfcbank.net>","subject":"Statement ready","date":"2026-10-13 09:15","unread":False,"snippet":"Your October statement is ready to view."},
            {"id":"m3","from":"Priya <priya@example.test>","subject":"FYI: offsite dates","date":"2026-10-12 18:40","unread":True,"snippet":"Offsite is 28-29 Oct, no action needed."},
        ])
        chat([
            {"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] Check my latest emails and give me updates"},
            {"role":"assistant","content":"","tool_calls":m1["tool_calls"]},
            {"role":"tool","content":rows},
        ], "1b. after tool result -> expect grouped brief, no invented mail")

    # 2. ambiguous Andy
    m2 = chat([{"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] Add a call with Andy tomorrow at 3pm"}], "2. ambiguous Andy -> expect question or find_contact, NOT a guessed draft")

    # 3. disambiguated -> draft, then confirm -> create
    m3 = chat([
        {"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] Add a call with Andy tomorrow at 3pm"},
        {"role":"assistant","content":"You have two Andys: Andy Chen and Andy Patel. Which one?"},
        {"role":"user","content":"[now: 2026-10-13T14:31 Asia/Kolkata] Andy Chen"},
    ], "3. 'Andy Chen' -> expect draft_calendar_event with 2026-10-14T15:00 and andy.chen@example.test")

    if m3.get("tool_calls"):
        draft = json.dumps({"draft_id":"d1","summary":"Call with Andy Chen","start":"2026-10-14T15:00+05:30","end":"2026-10-14T15:30+05:30","attendees":["andy.chen@example.test"],"status":"DRAFT, not created. Ask the user to confirm."})
        m3b = chat([
            {"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] Add a call with Andy tomorrow at 3pm"},
            {"role":"assistant","content":"You have two Andys: Andy Chen and Andy Patel. Which one?"},
            {"role":"user","content":"[now: 2026-10-13T14:31 Asia/Kolkata] Andy Chen"},
            {"role":"assistant","content":"","tool_calls":m3["tool_calls"]},
            {"role":"tool","content":draft},
        ], "3b. after draft result -> expect it to SHOW the draft and wait, NOT call create")
        chat([
            {"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] Add a call with Andy tomorrow at 3pm"},
            {"role":"assistant","content":"You have two Andys: Andy Chen and Andy Patel. Which one?"},
            {"role":"user","content":"[now: 2026-10-13T14:31 Asia/Kolkata] Andy Chen"},
            {"role":"assistant","content":"","tool_calls":m3["tool_calls"]},
            {"role":"tool","content":draft},
            {"role":"assistant","content":"Draft: Call with Andy Chen, Wed 14 Oct 15:00-15:30 IST, attendee andy.chen@example.test. Shall I create it?"},
            {"role":"user","content":"[now: 2026-10-13T14:32 Asia/Kolkata] yes"},
        ], "3c. 'yes' -> expect create_calendar_event(draft_id='d1')")

    # 4. past date
    chat([{"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] Also book Andy Patel for yesterday at 3pm"}], "4. past date -> expect text refusal or a draft call the validator will reject")

    # 5. premature 'yes' with nothing pending
    chat([{"role":"user","content":"[now: 2026-10-13T14:30 Asia/Kolkata] yes go ahead"}], "5. 'yes' with no draft -> expect a question, NOT create_calendar_event")
