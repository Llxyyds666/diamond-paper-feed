"""Nonfatal command-line delivery of a published notification plan."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys

from diamond_feed.bark import send_plan
from diamond_feed.notification import load_notification_plan


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid notification configuration")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--timeout-seconds", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        timeout_seconds = 10.0
        if args.timeout_seconds is not None:
            timeout_seconds = float(args.timeout_seconds)
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise ValueError("invalid notification configuration")
    except (TypeError, ValueError):
        print("Invalid notification configuration; Bark delivery skipped.", file=sys.stderr)
        return 0

    try:
        plan = load_notification_plan(Path(args.plan))
    except (OSError, TypeError, ValueError):
        print("Invalid notification plan; Bark delivery skipped.", file=sys.stderr)
        return 0

    token = os.environ.get("BARK_TOKEN")
    if token is None or not token.strip():
        print("BARK_TOKEN is missing; Bark delivery skipped.", file=sys.stderr)
        return 0

    try:
        if args.timeout_seconds is None:
            send_plan(token, plan)
        else:
            send_plan(token, plan, timeout_seconds=timeout_seconds)
    except Exception:
        print("Bark delivery failed unexpectedly.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
