"""Eval suites write their tool and turn log rows to a temp folder, not logs/.

autonomy.py reads logs/tools.jsonl back to compute earned-promotion streaks, so test
rows in the real log would show up as fake approvals in the demo. Set
VAANI_EVAL_REAL_LOGS=1 to keep a run's rows in logs/ for inspection.
"""

import os
import tempfile
from pathlib import Path

# The suites test the code defaults, not whatever demo flags .env carries: python-dotenv
# never overrides a variable that is already set, so pinning them here wins. A suite that
# needs a flag on sets it itself.
for _key, _default in {
    "VAANI_AUTH": "single", "VAANI_ALLOWED_EMAILS": "", "VAANI_AUTONOMY": "0", "VAANI_AUTO_ALLOWED": "triage",
    "VAANI_PROMOTION": "0", "VAANI_PROMOTION_N": "5", "VAANI_BRIEF_ON_OPEN": "0", "VAANI_BRIEF_TTL_MIN": "10",
    "VAANI_BRIEF_DAYS": "", "VAANI_FOLLOWUPS": "0", "VAANI_FOLLOWUP_DAYS": "", "VAANI_THEME": "classic", "VAANI_DEMO_LABEL": "",
}.items():
    os.environ[_key] = _default

if os.environ.get("VAANI_EVAL_REAL_LOGS", "").strip() != "1":
    from assistant import obs

    _tmp = Path(tempfile.mkdtemp(prefix="vaani-eval-logs-"))
    obs.LOG_DIR = _tmp
    obs.TOOLS_LOG = _tmp / "tools.jsonl"
    obs.TURNS_LOG = _tmp / "turns.jsonl"
