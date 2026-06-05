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

The decision needs the current ISO week to be *present* in the supplied
records. A single oldest-first page (``sort=asc&per_page=100``) of a repo with
thousands of MRs never contains a current-week record, so the gate would skip
forever. ``select_gate_records`` defends against that: it sorts the supplied
records most-recent-first and keeps the newest window, so the latest week is
always reachable regardless of how the platform ordered or paginated them.

A second mode (``--mode no-pr-week``) backs the scheduled (cron) eval path:
the first-PR gate runs the eval only on a week that opened a PR, so a week with
NO PR would never run an eval at all. The cron fires that uncovered case as a
last-resort backstop. ``week_has_no_pr`` exits 0 (run) only when no PR was
opened this ISO week; the CI cron additionally pins this mode to the week's LAST
day (Sunday) so it cannot preempt the PR path mid-week. The shared marker is
the PR list itself — neither path consumes state, so both stay order-independent
and re-run safe. The end-of-week pin makes a double-run a sub-day Sunday-only
sliver (a PR opened after the cron run), not a multi-day window; the zero-run
direction (a PR-less week never runs an eval) is fully eliminated.
"""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterable
from operator import itemgetter
from pathlib import Path

DEFAULT_PER_PAGE = 100


def _iso_week(moment: dt.datetime) -> tuple[int, int]:
    cal = moment.isocalendar()
    return cal[0], cal[1]


def _parse_created_at(raw: str) -> dt.datetime:
    text = raw.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def select_gate_records(
    mrs: Iterable[dict],
    *,
    now: dt.datetime | None = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> list[dict]:
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    target_week = _iso_week(now)
    dated: list[tuple[dt.datetime, dict]] = []
    current_week: list[dict] = []
    for mr in mrs:
        created = mr.get("created_at")
        if not created:
            continue
        try:
            created_at = _parse_created_at(str(created))
        except ValueError:
            continue
        dated.append((created_at, mr))
        if _iso_week(created_at) == target_week:
            current_week.append(mr)
    newest_first = [mr for _, mr in sorted(dated, key=itemgetter(0), reverse=True)]
    window = newest_first[:per_page]
    seen = {id(mr) for mr in window}
    window.extend(mr for mr in current_week if id(mr) not in seen)
    return window


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


def week_has_no_pr(
    mrs: Iterable[dict],
    *,
    now: dt.datetime | None = None,
) -> bool:
    """True iff NO MR/PR was opened in the current ISO week.

    The scheduled (cron) eval path uses this so it fires ONLY for a PR-less
    week — the last-resort backstop. The two paths share one marker, the
    week's PR list: a week with at least one PR is covered by the first-PR
    path, so the cron skips it; a week with NO PR is uncovered, so the cron
    runs. The CI cron additionally pins this mode to the week's LAST day
    (Sunday) — without that pin, a mid-week cron run followed by a later
    first PR the SAME week would double-fire (the PR list this function
    reads changes after the cron reads it). The end-of-week pin shrinks
    that to a Sunday-only sub-day sliver; the zero-run direction (a PR-less
    week never runs) is fully eliminated.
    """
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    target_week = _iso_week(now)
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
            return False
    return True


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mrs-file", required=True, help="JSON file: list of {iid|number, created_at} MR records.")
    parser.add_argument(
        "--mode",
        choices=("first-of-week", "no-pr-week"),
        default="first-of-week",
        help="'first-of-week' (PR path, needs --current-iid) or 'no-pr-week' (cron path: run iff no PR this week).",
    )
    parser.add_argument("--current-iid", type=int, default=None, help="iid/number of the MR this pipeline is for.")
    parser.add_argument("--skip-code", type=int, default=1, help="Exit code when the eval should be skipped.")
    parser.add_argument("--now", default=None, help="Override 'now' (ISO-8601); for testing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    mrs = json.loads(Path(args.mrs_file).read_text(encoding="utf-8"))
    if not isinstance(mrs, list):
        print("--mrs-file must contain a JSON list", file=sys.stderr)
        return 2
    now = _parse_created_at(args.now) if args.now else None
    if args.mode == "no-pr-week":
        if week_has_no_pr(mrs, now=now):
            print("no PR opened this ISO week → run the weekly eval on the cron")
            return 0
        print("a PR opened this ISO week (first-PR path covers it) → skip the cron eval")
        return args.skip_code
    if args.current_iid is None:
        print("--current-iid is required in first-of-week mode", file=sys.stderr)
        return 2
    records = select_gate_records(mrs, now=now)
    if is_first_mr_of_week(records, current_iid=args.current_iid, now=now):
        print(f"first MR of the ISO week → run the weekly eval (iid={args.current_iid})")
        return 0
    print(f"not the first MR of the ISO week → skip (iid={args.current_iid})")
    return args.skip_code


if __name__ == "__main__":
    raise SystemExit(main())
