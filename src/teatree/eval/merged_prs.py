r"""Decide whether ANY PR merged in the lookback window (the scheduled-eval pre-check).

The metered behavioral-eval suite runs on a weekly cron. There is no point
spending API budget when nothing new merged since the last run, so the scheduled
path runs this pre-check first: given the repo's merged PRs (their ``merged_at``
timestamps) and a lookback window in days, it answers whether ANY PR merged
inside the window.

* ``any_merged_since`` is ``True``  → at least one PR merged in the window → run the eval.
* ``any_merged_since`` is ``False`` → nothing merged in the window → skip cleanly.

This is decoupled from PRs/ISO-weeks: it is a pure "is there anything new since
the last weekly run" check, the simplest honest mechanism for the cron path.

CRITICAL: this is a PRE-CHECK that decides whether to invoke the eval at all. It
is NOT a skip-as-pass inside the eval. Once the eval is invoked,
``--require-executed`` still makes it fail loud if it cannot execute (missing
key/binary). The decision is order-independent and re-run safe: it reads only
timestamps and mutates no state.

The PR list is read from a JSON file (the platform query ``gh api`` / ``glab
api`` stays in the CI YAML) so this stays a pure, unit-testable decision
function. Each entry needs a ``merged_at`` (ISO-8601); an unmerged PR
(``merged_at`` null/absent) is ignored.

This is the shared core behind both ``t3 eval merged-prs-since`` (the reusable
overlay-facing CLI — an overlay previously had to DUPLICATE this whole module)
and ``scripts/eval/merged_prs_since.py`` (the thin script the host workflow
shells out to). Both delegate here; the logic lives once.
"""

import datetime as dt
from collections.abc import Iterable

DEFAULT_DAYS = 7


def parse_ts(raw: str) -> dt.datetime:
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
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    cutoff = now - dt.timedelta(days=days)
    for pr in prs:
        merged = pr.get("merged_at")
        if not merged:
            continue
        try:
            merged_at = parse_ts(str(merged))
        except ValueError:
            continue
        if merged_at >= cutoff:
            return True
    return False
