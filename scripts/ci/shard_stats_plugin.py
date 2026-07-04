"""Pytest plugin: record this shard's collected/selected counts for the combiner.

Loaded only in the CI ``test-shard`` matrix (``-p scripts.ci.shard_stats_plugin``)
alongside ``pytest-split``. A collection hookwrapper captures the FULL collected
count before pytest-split narrows ``items`` to the group's slice, and the SELECTED
count after. ``scripts/ci/check_shard_completeness.py`` reads the emitted JSON in
the combiner to prove the shards partition the suite exactly once — a shard that
silently drops tests (sum < total) or a duplicated group (sum > total) then fails
the required ``test (3.13)`` gate LOUD instead of riding a green coverage number.
"""

import json
from collections.abc import Generator
from pathlib import Path

import pytest

_OUT_OPTION = "--shard-stats-out"


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


# hookwrapper (not the new-style wrapper) so the plugin brackets pytest-split's
# in-place ``items`` narrowing without having to return the hook result: the
# collected count is read before ``yield``, the selected count after.
@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> Generator[None, object]:
    total_collected = len(items)
    yield
    out = config.getoption(_OUT_OPTION)
    if out:
        payload = {
            "total_collected": total_collected,
            "selected": len(items),
            "group": _int_option(config, "--group"),
            "splits": _int_option(config, "--splits"),
        }
        Path(out).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
