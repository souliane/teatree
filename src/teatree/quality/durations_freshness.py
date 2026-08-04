"""How long ago a refresh of ``dev/.test_durations`` last landed (#4130).

``durations_coverage`` answers how much of the tree the file knows about, and its
shortfall is deliberately a WARN: at 11% coverage a FAIL would be a standing daily
page for a state that clears only when a refresh PR is merged, and a pager that
fires nightly for something nobody caused trains its reader to ignore it. But that
left the epic's own failure mode unalarmed — when the daily ``refresh-durations``
job stops producing, the file simply ages, coverage rots, and every surface reads
green.

Age separates the two states the coverage number conflates. A file that is
incomplete *while the pipeline runs* is expected and self-clearing; a file no
refresh has touched for weeks is a stopped pipeline, which is actionable and
belongs on the pager. A refresh that lands resets the age to zero even while
coverage is still climbing, so this can never re-page the state the WARN exists to
tolerate.

The observable is the committer date of the newest commit that touched the file,
not its mtime: a re-provisioned deploy or a fresh worktree stamps every file at
checkout time, so an mtime-keyed alarm would read a dead pipeline as freshly
refreshed forever — silence in exactly the case the alarm exists for. Per-entry
timestamps are not an option either; the file records seconds and nothing else.
"""

import dataclasses
import datetime as dt
from pathlib import Path

from teatree.quality.durations_file import DURATIONS_PATH
from teatree.utils.run import run_allowed_to_fail

#: Measured on this tree rather than picked for roundness. `MIN_FILE_COVERAGE` calls
#: a file unhealthy below 80% coverage; against 2,226 test files that is a margin of
#: ~445 unrecorded files, and `origin/main` adds 36.6 / 35.8 / 31.9 new
#: ``tests/**/test_*.py`` files per day over trailing 7 / 14 / 30-day windows — so a
#: file refreshed to ~100% decays through that floor by ordinary churn alone in
#: 12.2 / 12.4 / 13.9 days. Twelve is the floor of that band: the first age at which
#: silence alone guarantees the split is blind for a fifth of the suite. Shorter and
#: an unattended week with the refresh PR open but unmerged pages on the day the owner
#: returns; a fortnight is past the decay point, so the alarm would arrive after the
#: blindness it exists to prevent.
MAX_REFRESH_AGE_DAYS = 12

_GIT_TIMEOUT_SECONDS = 10.0


@dataclasses.dataclass(frozen=True)
class DurationsFreshness:
    landed_at: dt.datetime
    measured_at: dt.datetime

    @property
    def age_days(self) -> int:
        return (self.measured_at - self.landed_at).days

    @property
    def is_stale(self) -> bool:
        return self.age_days >= MAX_REFRESH_AGE_DAYS


def measure_durations_freshness(repo: Path, *, now: dt.datetime | None = None) -> DurationsFreshness | None:
    """Return when *repo*'s durations file last changed, or ``None`` when that is unknowable.

    ``None`` is "this venue cannot answer" — no checkout, no git, a history that never
    recorded the file (a shallow clone), an unparsable date. It is never "fresh": the
    verdict this feeds pages the owner, so it must rest on an age positively established.
    """
    if not (repo / DURATIONS_PATH).is_file():
        return None
    landed = _last_landing_iso(repo)
    if not landed:
        return None
    try:
        landed_at = dt.datetime.fromisoformat(landed)
    except ValueError:
        return None
    return DurationsFreshness(landed_at=landed_at, measured_at=now or dt.datetime.now(dt.UTC))


def _last_landing_iso(repo: Path) -> str:
    """The committer date of the newest commit touching the durations file, or ``""``."""
    try:
        result = run_allowed_to_fail(
            ["git", "log", "-1", "--format=%cI", "--", DURATIONS_PATH.as_posix()],
            cwd=repo,
            expected_codes=None,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — a doctor check must never crash the run
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
