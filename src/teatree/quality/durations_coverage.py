"""How much of the test tree ``dev/.test_durations`` actually knows about (#4048).

pytest-split does not skip a test it has no duration for — it bin-packs it at the
average of the ones it does (``durations.get(nodeid, avg_duration_per_test)``).
So a durations file covering a fraction of the suite produces a split that is
*confidently* wrong: the twelve shards look balanced to the algorithm while the
slowest unrecorded tests cluster wherever collection happens to put them. The
shard that draws them exceeds the per-test ceiling, and the PR held red is
whichever one was in flight — a red its own diff cannot have caused, moving
between shards run to run because contention, not a regression, produced it.

Coverage is counted in FILES rather than node ids on purpose: node ids need a
full ``pytest --collect-only`` (measured: 144s on this tree), which is far too
slow for a check that runs on every session start, while the set of
``tests/**/test_*.py`` paths is a glob. The two answer the same question — a file
absent from the durations file has *none* of its tests recorded — and the file
count is exact rather than sampled.
"""

import dataclasses
import json
from pathlib import Path

# A refreshed file records every test that ran, so healthy coverage is ~100%; a
# day of churn between daily refreshes moves it by a handful of files out of
# thousands. The floor is set well below that so ordinary churn never fires it
# and only a refresh pipeline that has stopped producing does.
MIN_FILE_COVERAGE = 0.80

_DURATIONS_PATH = Path("dev") / ".test_durations"
_TESTS_DIR = "tests"


class DurationsUnreadableError(RuntimeError):
    """The durations file exists but does not parse — a read failure, not an empty file.

    Degrading this to "no durations recorded" would report a parse error as a
    coverage figure, which is the one reading an operator cannot act on.
    """


@dataclasses.dataclass(frozen=True)
class DurationsCoverage:
    covered_files: int
    test_files: int
    orphan_keys: int

    @property
    def ratio(self) -> float:
        return self.covered_files / self.test_files if self.test_files else 1.0

    @property
    def is_healthy(self) -> bool:
        return self.ratio >= MIN_FILE_COVERAGE


def measure_durations_coverage(repo: Path) -> DurationsCoverage | None:
    """Return the coverage of *repo*'s durations file, or ``None`` if it has no test tree.

    ``None`` is "this venue cannot answer" (an installed teatree with no
    checkout), never "healthy" — the caller stays silent rather than reporting a
    verdict it did not establish.
    """
    tests_dir = repo / _TESTS_DIR
    if not tests_dir.is_dir():
        return None

    test_files = {path.relative_to(repo).as_posix() for path in tests_dir.glob("**/test_*.py")}
    if not test_files:
        return None

    recorded = _load_recorded_node_ids(repo / _DURATIONS_PATH)
    recorded_files = {node_id.split("::", 1)[0] for node_id in recorded}
    return DurationsCoverage(
        covered_files=len(recorded_files & test_files),
        test_files=len(test_files),
        orphan_keys=sum(1 for node_id in recorded if not (repo / node_id.split("::", 1)[0]).is_file()),
    )


def _load_recorded_node_ids(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"{path} exists but could not be read as durations JSON: {exc}"
        raise DurationsUnreadableError(message) from exc
    if not isinstance(data, dict):
        message = f"{path} is not a durations mapping (got {type(data).__name__})"
        raise DurationsUnreadableError(message)
    return [str(key) for key in data]
