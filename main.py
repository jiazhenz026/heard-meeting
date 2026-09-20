"""The entry point. One command starts the whole application.

    python main.py [--port N] [--no-audio] [--no-frontdesk]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys

import uvicorn

from heard.config import CONFIG, Config
from heard.server.app import BOARD_DIST, Settings, create_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="heard", description="Heard! — the meeting board that listens.")
    p.add_argument("--port", type=int, default=CONFIG.port)
    p.add_argument("--host", default=CONFIG.host)
    p.add_argument("--no-audio", action="store_true", help="log spoken lines instead of synthesising them")
    p.add_argument("--no-frontdesk", action="store_true", help="cards and notes only; nothing is ever spoken")
    p.add_argument("--log-level", default="info", choices=("debug", "info", "warning", "error"))
    return p.parse_args(argv)


def validate(config: Config, args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if not config.has_nvidia:
        problems.append("NVIDIA_API_KEY is unset — no classifier, no cards, no notes")
    if config.frontdesk != "off" and not args.no_frontdesk and shutil.which("claude") is None:
        problems.append("`claude` CLI not on PATH — the front desk and researchers run on the Claude Agent SDK "
                        "(npm i -g @anthropic-ai/claude-code, then `claude login` or set ANTHROPIC_API_KEY)")
    return problems


def banner(config: Config, args: argparse.Namespace) -> None:
    print("Heard! — A meeting board that joins the conversation and conducts research in real time.\n")
    for line in config.report():
        print(line)
    if args.no_audio:
        print("  speech out  DISABLED (--no-audio)")
    print(f"  board       {BOARD_DIST if BOARD_DIST.is_dir() else 'NOT BUILT — cd board && npm run build'}")
    print(f"  data        {config.data_dir}")
    print(f"\n  http://{args.host}:{args.port}\n", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s", datefmt="%H:%M:%S")
    if args.no_frontdesk:
        CONFIG.frontdesk = "off"
    problems = validate(CONFIG, args)
    banner(CONFIG, args)
    if problems:
        print("Refusing to start:", file=sys.stderr)
        for pr in problems:
            print(f"  · {pr}", file=sys.stderr)
        return 2
    app = create_app(Settings(host=args.host, port=args.port, audio=not args.no_audio))
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
                                           log_level=args.log_level, access_log=False))
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
