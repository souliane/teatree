"""The newest SCHEDULED CI run, read off ``gh run list`` output (#4477).

A scheduled run reds nothing anyone watches. `refresh-durations` failed on every daily
run for eleven days while the only surface reporting it was a doctor line about the
*symptom* — the durations file ageing — so the cause stayed invisible and every PR paid
for it in an unbalanced shard split.

The parse is deliberately strict about one distinction. "The forge answered with no
scheduled runs" and "the read failed" are different answers, and a reader that returns an
empty for both reports a rejected credential as a healthy schedule — the exact shape of
the original outage, where an upload that found nothing exited success.
"""

import dataclasses
import datetime as dt
import json
from typing import TypedDict, cast

#: Conclusions that mean the scheduled run BROKE. `cancelled`/`skipped`/`neutral` are
#: outcomes nobody needs to act on, so they must not page.
_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

_REQUIRED_FIELDS = ("databaseId", "status", "conclusion", "createdAt", "url")


class RunRow(TypedDict, total=False):
    """One ``gh run list --json`` row. ``total=False``: a field the forge omitted is the case being caught."""

    databaseId: int
    status: str
    conclusion: str
    createdAt: str
    url: str


class ScheduledRunUnreadableError(RuntimeError):
    """The run list could not be read — never reported as "no runs"."""


@dataclasses.dataclass(frozen=True)
class ScheduledRun:
    run_id: int
    status: str
    conclusion: str
    created_at: dt.datetime
    url: str

    @property
    def failed(self) -> bool:
        return self.conclusion in _FAILING_CONCLUSIONS


def _parse_row(row: RunRow) -> ScheduledRun:
    missing = [field for field in _REQUIRED_FIELDS if field not in row]
    if missing:
        message = f"run list row is missing {', '.join(missing)} — asked gh for the wrong fields?"
        raise ScheduledRunUnreadableError(message)
    try:
        created_at = dt.datetime.fromisoformat(str(row.get("createdAt", "")))
    except ValueError as exc:
        message = f"run list row carries an unreadable createdAt: {exc}"
        raise ScheduledRunUnreadableError(message) from exc
    return ScheduledRun(
        run_id=int(str(row.get("databaseId", 0))),
        status=str(row.get("status", "")),
        conclusion=str(row.get("conclusion", "")),
        created_at=created_at,
        url=str(row.get("url", "")),
    )


def newest_scheduled_run(payload: str) -> ScheduledRun | None:
    """The most recent run in *payload*, or ``None`` when the schedule has genuinely never run.

    *payload* is raw ``gh run list --json …`` stdout. Anything that is not a JSON list of
    rows raises rather than degrading to ``None``.
    """
    try:
        rows = json.loads(payload or "")
    except (json.JSONDecodeError, TypeError) as exc:
        message = f"run list is not JSON: {exc}"
        raise ScheduledRunUnreadableError(message) from exc
    if not isinstance(rows, list):
        message = f"run list is a {type(rows).__name__}, not a list of runs"
        raise ScheduledRunUnreadableError(message)
    parsed = [_parse_row(cast("RunRow", row)) for row in rows if isinstance(row, dict)]
    if len(parsed) != len(rows):
        message = "run list holds an entry that is not a run object"
        raise ScheduledRunUnreadableError(message)
    if not parsed:
        return None
    return max(parsed, key=lambda run: run.created_at)
