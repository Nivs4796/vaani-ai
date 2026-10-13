import json, time, urllib.request, sys
sys.path.insert(0, ".")
from ollama_probe import TOOLS, MODEL, URL  # schemas shared with probe 1 (guarded by __main__)

def chat(system, messages, temperature=0):
    body = {"model": MODEL, "messages": [{"role":"system","content":system}] + messages, "tools": TOOLS, "stream": False, "options": {"temperature": temperature}, "keep_alive": "30m"}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.load(r)
    msg = out["message"]
    calls = [(c["function"]["name"], c["function"].get("arguments")) for c in (msg.get("tool_calls") or [])]
    return calls, msg.get("content","").strip(), time.time()-t0

BASE = """You are a personal assistant for Alex. Timezone: Asia/Kolkata.
Known contacts: Andy Chen <andy.chen@example.test>, Andy Patel <andy.patel@example.test>.
Rules:
- Never claim to have read email or the calendar without calling a tool.
- To schedule something: resolve the person first. If a name matches two or more contacts, ask one short question which one; do not guess.
- Then call draft_calendar_event and show the draft. Do NOT call create_calendar_event until the user explicitly confirms (yes / confirm / go ahead).
- Keep replies short and plain."""

TIGHT = BASE + """
- create_calendar_event takes ONLY a draft_id that a previous draft_calendar_event result returned in this conversation. If there is no such draft, say "There is no pending draft" and ask what to schedule. Never invent a draft_id.
- If the requested time is in the past relative to [now], do not call any tool; tell the user it is in the past and ask for a future time."""

NOW = "[now: 2026-10-13T14:30 Asia/Kolkata] "

def show(label, calls, text, wall):
    c = "; ".join(f"{n}({json.dumps(a)})" for n,a in calls) or "-"
    print(f"{label:<34} {wall:4.1f}s  calls: {c}  text: {text[:90]!r}")

print("\n### A. find_contact returned two Andys -> should ASK, not draft (BASE prompt, 3 runs at temp 0.7)")
two = json.dumps([{"name":"Andy Chen","email":"andy.chen@example.test"},{"name":"Andy Patel","email":"andy.patel@example.test"}])
for i in range(3):
    calls, text, wall = chat(BASE, [
        {"role":"user","content":NOW+"Add a call with Andy tomorrow at 3pm"},
        {"role":"assistant","content":"","tool_calls":[{"function":{"name":"find_contact","arguments":{"name":"Andy"}}}]},
        {"role":"tool","content":two},
    ], temperature=0.7)
    show(f"A run {i+1}", calls, text, wall)

print("\n### B. past date, BASE vs TIGHT prompt, 3 runs each at temp 0.7")
for name, sysp in (("BASE", BASE), ("TIGHT", TIGHT)):
    for i in range(3):
        calls, text, wall = chat(sysp, [{"role":"user","content":NOW+"Also book Andy Patel for yesterday at 3pm"}], temperature=0.7)
        show(f"B {name} run {i+1}", calls, text, wall)

print("\n### C. 'yes go ahead' with no pending draft, BASE vs TIGHT, 3 runs each at temp 0.7")
for name, sysp in (("BASE", BASE), ("TIGHT", TIGHT)):
    for i in range(3):
        calls, text, wall = chat(sysp, [{"role":"user","content":NOW+"yes go ahead"}], temperature=0.7)
        show(f"C {name} run {i+1}", calls, text, wall)

print("\n### D. happy path stability: 'Andy Chen' after the which-Andy question, TIGHT, 3 runs at temp 0.7")
for i in range(3):
    calls, text, wall = chat(TIGHT, [
        {"role":"user","content":NOW+"Add a call with Andy tomorrow at 3pm"},
        {"role":"assistant","content":"You have two Andys: Andy Chen and Andy Patel. Which one?"},
        {"role":"user","content":"[now: 2026-10-13T14:31 Asia/Kolkata] Andy Chen"},
    ], temperature=0.7)
    show(f"D run {i+1}", calls, text, wall)
