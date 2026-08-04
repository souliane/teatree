"""Pytest plugin: record what the combiner needs to judge this shard.

Loaded only in the CI ``test-shard`` matrix (``-p scripts.ci.shard_stats_plugin``)
alongside ``pytest-split``. Two records land in one JSON:

**Partition counts.** A collection hookwrapper captures the FULL collected count
before pytest-split narrows ``items`` to the group's slice, and the SELECTED count
after. ``scripts/ci/check_shard_completeness.py`` reads them in the combiner to
prove the shards partition the suite exactly once — a shard that silently drops
tests (sum < total) or a duplicated group (sum > total) then fails the required
``test (3.13)`` gate LOUD instead of riding a green coverage number. They are
written at collection time, so a session that dies mid-run still leaves them.

**Seconds against the ceiling that applied.** The lane raises the per-test timeout
because twelve contending shards cost more than the tight local ceiling was sized
for (#4048); a raise nobody measures only moves the cliff, so each test's real
cost is recorded against its own effective ceiling — a ``@pytest.mark.timeout``
where one is stated, the lane value otherwise, matching how
``pytest_timeout._get_item_settings`` resolves it. Only the tightest few per shard
are kept: ``scripts/ci/report_timeout_headroom.py`` merges the twelve and names
whatever is running out of room, as a report and never a gate.
"""

import json
from collections import defaultdict
from collections.abc import Generator
from pathlib import Path
from typing import TypedDict

import pytest

_OUT_OPTION = "--shard-stats-out"

# Enough that the merged twelve cover any plausible tail, small enough that the
# artifact stays a summary rather than a copy of the durations file.
_KEEP_PER_SHARD = 20


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        _OUT_OPTION,
        action="store",
        default=None,
        help="Write shard collection stats (total/selected/group/splits) as JSON to this path.",
    )


def _int_option(config: pytest.Config, name: str) -> int | None:
    # pytest-split registers --group / --splits; getoption raises ValueError
    # when the option is unknown (plugin absent), so a missing split is None.
    try:
        value = config.getoption(name)
    except ValueError:
        return None
    return int(value) if value is not None else None


def _effective_ceiling(item: pytest.Item) -> float | None:
    """The timeout that applies to *item*, resolved the way pytest-timeout resolves it.

    A marker wins over the lane value, and ``_env_timeout`` is where pytest-timeout
    has already folded the env var over the ini one — reading the ini directly would
    disagree with the ceiling actually enforced. Absent pytest-timeout there is no
    ceiling to measure against, and ``None`` keeps the test out of the record rather
    than inventing one.
    """
    marker = item.get_closest_marker("timeout")
    if marker is not None:
        stated = marker.kwargs.get("timeout", marker.args[0] if marker.args else None)
        if isinstance(stated, int | float):
            return float(stated)
    lane = getattr(item.config, "_env_timeout", None)
    return float(lane) if isinstance(lane, int | float) and lane > 0 else None


class HeadroomEntry(TypedDict):
    """One test's cost against the ceiling that applied to it, as it lands in the artifact."""

    node_id: str
    seconds: float
    ceiling: float


class ShardStatsFile(TypedDict):
    """The artifact contract `check_shard_completeness` and `report_timeout_headroom` read."""

    total_collected: int
    selected: int
    group: int | None
    splits: int | None
    slowest_against_ceiling: list[HeadroomEntry]


class ShardStats:
    """This shard's two records, and the only writer of the stats file."""

    def __init__(self) -> None:
        self.total_collected = 0
        self.selected = 0
        self.ceilings: dict[str, float] = {}
        self.seconds: defaultdict[str, float] = defaultdict(float)

    def payload(self, config: pytest.Config) -> ShardStatsFile:
        return ShardStatsFile(
            total_collected=self.total_collected,
            selected=self.selected,
            group=_int_option(config, "--group"),
            splits=_int_option(config, "--splits"),
            slowest_against_ceiling=self.tightest(),
        )

    def tightest(self) -> list[HeadroomEntry]:
        measured = [
            (seconds / self.ceilings[node_id], node_id, seconds)
            for node_id, seconds in self.seconds.items()
            if node_id in self.ceilings
        ]
        measured.sort(key=lambda entry: (-entry[0], entry[1]))
        return [
            HeadroomEntry(node_id=node_id, seconds=round(seconds, 3), ceiling=self.ceilings[node_id])
            for _consumed, node_id, seconds in measured[:_KEEP_PER_SHARD]
        ]

    def write(self, config: pytest.Config) -> None:
        out = config.getoption(_OUT_OPTION)
        if out:
            Path(out).write_text(json.dumps(self.payload(config), indent=2, sort_keys=True), encoding="utf-8")


_STATS = ShardStats()


# hookwrapper (not the new-style wrapper) so the plugin brackets pytest-split's
# in-place ``items`` narrowing without having to return the hook result: the
# collected count is read before ``yield``, the selected count after.
@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> Generator[None, object]:
    _STATS.total_collected = len(items)
    yield
    _STATS.selected = len(items)
    for item in items:
        ceiling = _effective_ceiling(item)
        if ceiling is not None:
            _STATS.ceilings[item.nodeid] = ceiling
    _STATS.write(config)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # Setup and teardown count: the ceiling covers the whole item unless
    # ``timeout_func_only`` is set, so a class-scoped fixture's cost is part of
    # what its first test spends against it.
    _STATS.seconds[report.nodeid] += report.duration


def pytest_sessionfinish(session: pytest.Session) -> None:
    _STATS.write(session.config)
