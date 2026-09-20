"""Replay a scripted meeting into a running Heard! through POST /inject.

    python scripts/replay.py fixtures/demo.txt [--url http://127.0.0.1:8000] [--speed 1.0]

Lines look like `+3  J: text`. `+N` is the wait in seconds before the line.
The whole test harness runs on this: no microphone in the loop.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

LINE = re.compile(r"^\+(\d+(?:\.\d+)?)\s+([A-Za-z?]+):\s*(.+)$")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("fixture", type=Path)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--speed", type=float, default=1.0, help=">1 is faster")
    p.add_argument("--reset", action="store_true", help="POST /reset first")
    args = p.parse_args()

    if args.reset:
        httpx.post(f"{args.url}/reset", timeout=10)
    started = time.time()
    for raw in args.fixture.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        m = LINE.match(raw)
        if not m:
            print(f"skip: {raw}", file=sys.stderr)
            continue
        wait, speaker, text = float(m.group(1)), m.group(2), m.group(3)
        time.sleep(wait / args.speed)
        r = httpx.post(f"{args.url}/inject", json={"text": text, "speaker": speaker}, timeout=10)
        print(f"[{time.time() - started:6.1f}s] {speaker}: {text[:70]}  → {r.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
