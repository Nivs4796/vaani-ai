"""Voice latency on this Mac: warm the speech model, time three transcriptions of each
fixture in static/test/ and one `say` synthesis, and print a small table.

Run: HF_HUB_OFFLINE=1 .venv/bin/python -m spike.smoke_voice
"""

from __future__ import annotations

import logging
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from assistant import voice  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "static" / "test"
FILES = ("voice-q1.wav", "voice-q2.wav", "voice-q1.webm")
SPOKEN = "Ravi Mehta wants the quote by Friday. I've drafted a reply. Tap Send when you're ready."


def main() -> int:
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    t0 = time.perf_counter()
    voice.start_warmup()
    while voice.status()["stt"] == "loading":
        time.sleep(0.05)
    st = voice.status()
    if st["stt"] != "ready":
        print(f"speech-to-text not ready: {st}")
        return 1
    print(f"model {st['model']} ({voice.stt_compute()}, {voice.stt_threads()} threads): "
          f"loaded and warmed in {st['load_ms']} ms (wall {int((time.perf_counter() - t0) * 1000)} ms)\n")
    print(f"{'input':<16}{'kB':>6}{'run 1':>8}{'run 2':>8}{'run 3':>8}{'median':>8}  text")
    for name in FILES:
        data = (FIXTURES / name).read_bytes()
        runs = [voice.transcribe(data) for _ in range(3)]
        ms = [r["ms"] for r in runs]
        print(f"{name:<16}{len(data) / 1024:>6.0f}{ms[0]:>8}{ms[1]:>8}{ms[2]:>8}{int(statistics.median(ms)):>8}  {runs[-1]['text']!r}")
    t1 = time.perf_counter()
    wav = voice.synth(SPOKEN)
    ms = int((time.perf_counter() - t1) * 1000)
    print(f"{'say (tts)':<16}{len(wav) / 1024:>6.0f}{ms:>8}{'':>8}{'':>8}{ms:>8}  {len(SPOKEN)} chars, voice {voice.resolve_voice(None)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
