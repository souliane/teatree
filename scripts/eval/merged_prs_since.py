r"""Decide whether ANY PR merged in the lookback window (the scheduled-eval pre-check).

The metered behavioral-eval suite runs on a weekly cron. There is no point
spending API budget when nothing new merged since the last run, so the scheduled
path runs this pre-check first: given the repo's merged PRs (their ``merged_at``
timestamps) and a lookback window in days, it answers whether ANY PR merged
inside the window.

* Exit 0  → at least one PR merged in the window → run the eval (new work to test).
* Exit ``--skip-code`` (default 1) → nothing merged in the window → skip cleanly.

This is decoupled from PRs/ISO-weeks: it is a pure "is there anything new since
the last weekly run" check, the simplest honest mechanism for the cron path. The
manual ``workflow_dispatch`` path does NOT use this guard — a maintainer forcing
a run always runs.

CRITICAL: this is a PRE-CHECK that decides whether to invoke the eval at all. It
is NOT a skip-as-pass inside the eval. Once the eval is invoked,
``--require-executed`` still makes it fail loud if it cannot execute (missing
key/binary). The decision is order-independent and re-run safe: it reads only
timestamps and mutates no state.

The PR list is read from a JSON file (``--prs-file``) so the platform query
(``gh api`` / ``glab api``) stays in the CI YAML and this script stays a pure,
unit-testable decision function. Each entry needs a ``merged_at`` (ISO-8601);
an unmerged PR (``merged_at`` null/absent) is ignored.
"""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterable
from pathlib import Path

DEFAULT_DAYS = 7


def _parse_ts(raw: str) -> dt.datetime:
    text = raw.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def any_merged_since(
    prs: Iterable[dict],
    *,
    now: dt.datetime | None = None,
    days: int = DEFAULT_DAYS,
) -> bool:
    """True iff at least one PR has a ``merged_at`` within ``days`` before ``now``."""
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    cutoff = now - dt.timedelta(days=days)
    for pr in prs:
        merged = pr.get("merged_at")
        if not merged:
            continue
        try:
            merged_at = _parse_ts(str(merged))
        except ValueError:
            continue
        if merged_at >= cutoff:
            return True
    return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prs-file", required=True, help="JSON file: list of {number, merged_at} PR records.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Lookback window in days (default: 7).")
    parser.add_argument("--skip-code", type=int, default=1, help="Exit code when the eval should be skipped.")
    parser.add_argument("--now", default=None, help="Override 'now' (ISO-8601); for testing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    prs = json.loads(Path(args.prs_file).read_text(encoding="utf-8"))
    if not isinstance(prs, list):
        print("--prs-file must contain a JSON list", file=sys.stderr)
        return 2
    now = _parse_ts(args.now) if args.now else None
    if any_merged_since(prs, now=now, days=args.days):
        print(f"a PR merged in the last {args.days} day(s) → run the weekly eval")
        return 0
    print(f"no PR merged in the last {args.days} day(s) — skipping, nothing new to test")
    return args.skip_code


if __name__ == "__main__":
    raise SystemExit(main())
