r"""Decide whether the current merge request is the FIRST opened this ISO week.

The weekly eval gate must run exactly once per ISO week — on the first MR
opened that week — not on every push and not on every MR. A pure CI ``rules:``
expression cannot express "first of the week", so this script does it: given
the project's MRs (their creation timestamps), it answers whether the current
MR is the earliest-created MR whose ``created_at`` falls in the current ISO
week.

The decision is **order-independent and re-run safe**: it does not consume a
marker or mutate state, so re-running a pipeline yields the same verdict, and a
later MR in the same week never flips an earlier one's verdict. When the current
MR genuinely is the first of the week the script exits 0 (run the eval);
otherwise it exits the ``--skip-code`` (default 1), which a CI ``allow_failure``
or explicit check treats as "skip".

The MR list is read from a JSON file (``--mrs-file``) so the platform query
(``glab api`` / ``gh api``) stays in the CI YAML and this script stays a pure,
unit-testable decision function. Each MR entry needs ``iid``/``number`` and
``created_at`` (ISO-8601). The current MR is identified by ``--current-iid``.
"""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterable
from pathlib import Path


def _iso_week(moment: dt.datetime) -> tuple[int, int]:
    cal = moment.isocalendar()
    return cal[0], cal[1]


def _parse_created_at(raw: str) -> dt.datetime:
    text = raw.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def is_first_mr_of_week(
    mrs: Iterable[dict],
    *,
    current_iid: int,
    now: dt.datetime | None = None,
) -> bool:
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    target_week = _iso_week(now)
    in_week: list[tuple[dt.datetime, int]] = []
    for mr in mrs:
        iid = mr.get("iid", mr.get("number"))
        created = mr.get("created_at")
        if iid is None or not created:
            continue
        try:
            created_at = _parse_created_at(str(created))
        except ValueError:
            continue
        if _iso_week(created_at) == target_week:
            in_week.append((created_at, int(iid)))
    if not in_week:
        return False
    _, earliest_iid = min(in_week)
    return earliest_iid == current_iid


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mrs-file", required=True, help="JSON file: list of {iid|number, created_at} MR records.")
    parser.add_argument("--current-iid", type=int, required=True, help="iid/number of the MR this pipeline is for.")
    parser.add_argument("--skip-code", type=int, default=1, help="Exit code when NOT the first MR of the week.")
    parser.add_argument("--now", default=None, help="Override 'now' (ISO-8601); for testing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    mrs = json.loads(Path(args.mrs_file).read_text(encoding="utf-8"))
    if not isinstance(mrs, list):
        print("--mrs-file must contain a JSON list", file=sys.stderr)
        return 2
    now = _parse_created_at(args.now) if args.now else None
    if is_first_mr_of_week(mrs, current_iid=args.current_iid, now=now):
        print(f"first MR of the ISO week → run the weekly eval (iid={args.current_iid})")
        return 0
    print(f"not the first MR of the ISO week → skip (iid={args.current_iid})")
    return args.skip_code


if __name__ == "__main__":
    raise SystemExit(main())
