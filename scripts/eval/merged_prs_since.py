r"""Decide whether ANY PR merged in the lookback window (the scheduled-eval pre-check).

The metered behavioral-eval suite runs on a weekly cron. There is no point
spending API budget when nothing new merged since the last run, so the scheduled
path runs this pre-check first. This script is the host-workflow shim around that
decision: it reads the repo's merged PRs from a JSON file and delegates to
:func:`teatree.eval.merged_prs.any_merged_since`, the shared core also exposed as
``t3 eval merged-prs-since`` for overlays to reuse (a downstream overlay
previously had to DUPLICATE this whole module).

* Exit 0  → at least one PR merged in the window → run the eval (new work to test).
* Exit ``--skip-code`` (default 1) → nothing merged in the window → skip cleanly.

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
import json
import sys
from pathlib import Path

from teatree.eval.merged_prs import DEFAULT_DAYS, any_merged_since, parse_ts

__all__ = ["any_merged_since", "main"]


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
    now = parse_ts(args.now) if args.now else None
    if any_merged_since(prs, now=now, days=args.days):
        print(f"a PR merged in the last {args.days} day(s) → run the weekly eval")
        return 0
    print(f"no PR merged in the last {args.days} day(s) — skipping, nothing new to test")
    return args.skip_code


if __name__ == "__main__":
    raise SystemExit(main())
