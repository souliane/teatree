"""How long ago ``dev/.test_durations`` was last refreshed (#4130).

The third reading of the artifact, and the only one that can tell that the refresh
pipeline has *stopped*. Coverage answers how much of the tree the file knows about,
which is a shortfall the daily refresh closes on its own — so it is an advisory the
operator is meant to watch climb, never a page. Age answers whether anything is
closing it at all, which nothing else on this surface can: a fully stale but perfectly
parseable file trips neither retained hard FAIL (the unreadable-file one needs
corruption, the over-run one needs a recorded over-run), so the pipeline could stop
indefinitely and the only signal would be a WARN that already stands.

Age is read from git rather than from the file, because the file is a
``node id -> seconds`` mapping with no timestamps in it, and its mtime is the mtime
of whatever checkout is asking — always minutes old in a freshly-created worktree.
The commit that last touched the artifact is "when a refresh last landed", which is
the question, and it survives re-checkout.
"""

import dataclasses
import datetime as dt
from pathlib import Path

from teatree.quality.durations_file import DURATIONS_PATH
from teatree.utils.git_run import run_with_status
from teatree.utils.git_worktree_query import is_git_checkout

# The refresh runs daily behind a drift gate and lands only once a human merges its
# PR, so days of quiet are ordinary. A fortnight is not: by then either the scheduled
# job has stopped producing or its PR is sitting unmerged, and both are worth a page.
MAX_REFRESH_AGE = dt.timedelta(days=14)


class DurationsHistoryUnreadableError(RuntimeError):
    """A checkout whose history git refused to read — an unverifiable age, not a fresh one.

    Kept distinct from "no commit records this artifact" so a git that cannot answer
    (dubious-ownership refusal, a broken gitdir pointer) surfaces as unverified instead
    of quietly holding the alarm down forever, which is the failure mode this whole
    check exists to remove one surface over.
    """


@dataclasses.dataclass(frozen=True)
class DurationsFreshness:
    last_refreshed_at: dt.datetime
    age: dt.timedelta

    @property
    def is_stale(self) -> bool:
        return self.age >= MAX_REFRESH_AGE


def measure_durations_freshness(repo: Path, *, now: dt.datetime) -> DurationsFreshness | None:
    """Return how long ago *repo* last committed a change to the durations artifact.

    ``None`` is "this venue holds no answer" — an installed teatree that is not a
    checkout, or a clone shallow enough that no commit in its history touched the
    artifact. Never "fresh": a caller that cannot establish the age reports nothing
    rather than a verdict it did not measure.
    """
    if not is_git_checkout(repo):
        return None

    result = run_with_status(repo=str(repo), args=["log", "-1", "--format=%cI", "--", DURATIONS_PATH.as_posix()])
    if result.returncode != 0:
        message = f"git could not read the history of {DURATIONS_PATH}: {result.stderr.strip()}"
        raise DurationsHistoryUnreadableError(message)

    stamp = result.stdout.strip()
    if not stamp:
        return None

    # `%cI` is strict ISO 8601 with an offset, so no parse guard: were git ever to emit
    # something else, the doctor check's crash handler reports it as a visible WARN, which
    # beats an unreachable branch here that no test can honestly cover.
    landed = dt.datetime.fromisoformat(stamp)
    return DurationsFreshness(last_refreshed_at=landed, age=now - landed)
