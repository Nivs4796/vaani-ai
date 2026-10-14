"""Run every offline suite and record the result for the Proof panel.

    .venv/bin/python -m evals.all

Each suite runs in its own process (they monkeypatch module globals) and prints a last
line like "106/106 manage checks passed". The counts land in data/eval_status.json,
which GET /proof shows as "offline checks: N of M passing". Exit code 1 on any failure.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT / "data" / "eval_status.json"
SUITES = ("test_manage", "test_guard", "test_attendees", "test_inbox", "test_auth", "test_autonomy",
          "test_promotion", "test_brief", "test_followups", "test_undo",
          "test_stream", "test_history", "test_rail", "test_voice", "test_voicestate", "test_wake", "test_voice_policy", "test_voiceturn", "test_voicecmd", "test_fastpath")
COUNT_RE = re.compile(r"(\d+)/(\d+)\b[^\n]*checks passed")


def run_suite(name: str) -> dict:
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", f"evals.{name}"], cwd=ROOT, capture_output=True, text=True, timeout=900)
    found = COUNT_RE.findall(proc.stdout or "")
    passed, total = (int(found[-1][0]), int(found[-1][1])) if found else (0, 0)
    ok = proc.returncode == 0 and total > 0 and passed == total
    return {"name": name.removeprefix("test_"), "passed": passed, "total": total, "ok": ok,
            "seconds": round(time.perf_counter() - t0, 1)}


def main() -> int:
    suites = []
    for name in SUITES:
        r = run_suite(name)
        suites.append(r)
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']:<11} {r['passed']}/{r['total']}  {r['seconds']}s")
    passed = sum(r["passed"] for r in suites)
    total = sum(r["total"] for r in suites)
    status = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "passed": passed, "total": total,
              "all_passed": all(r["ok"] for r in suites), "suites": suites}
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    print(f"\n{passed}/{total} offline checks passed across {len(suites)} suites -> {STATUS_FILE.relative_to(ROOT)}")
    return 0 if status["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
