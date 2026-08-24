"""Integration guard for the shard-stats pytest plugin.

Runs a real ``pytest`` subprocess over a toy suite and asserts the emitted JSON
carries both records the combiner reads: the partition counts
``scripts/ci/check_shard_completeness.py`` proves the shards cover the suite
exactly once with, and the per-test seconds-against-ceiling
``scripts/ci/report_timeout_headroom.py`` reads to say what the lane's raised
ceiling bought (#4048).

Only the partition half needs ``pytest-split`` (it ships in the opt-in ``shard``
dependency group, mirroring the shuffle-lane isolation), so the skip is per test
rather than module-wide — the headroom half must be runnable on a plain env.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_TOY_SUITE = """
import pytest


@pytest.mark.parametrize("i", range(10))
def test_toy(i):
    assert i >= 0
"""

_CEILING_SUITE = """
import pytest


def test_under_the_lane_ceiling():
    assert True


@pytest.mark.timeout(900)
def test_states_its_own_ceiling():
    assert True
"""


@pytest.mark.integration
def test_plugin_records_total_and_group_slice(tmp_path: Path) -> None:
    pytest.importorskip("pytest_split")
    (tmp_path / "test_toy.py").write_text(_TOY_SUITE, encoding="utf-8")
    stats_out = tmp_path / "shard-stats.1.json"

    env = _clean_env()
    env["PYTHONPATH"] = str(_REPO_ROOT)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_toy.py",
            "-p",
            "scripts.ci.shard_stats_plugin",
            "--splits",
            "2",
            "--group",
            "1",
            f"--shard-stats-out={stats_out}",
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert stats_out.exists(), completed.stdout + completed.stderr
    payload = json.loads(stats_out.read_text(encoding="utf-8"))
    assert payload["total_collected"] == 10, "the FULL collection must be recorded before the split"
    assert 0 < payload["selected"] < 10, "only the group's slice runs in this shard"
    assert payload["group"] == 1
    assert payload["splits"] == 2


@pytest.mark.integration
def test_plugin_records_each_test_against_the_ceiling_that_applied_to_it(tmp_path: Path) -> None:
    """The lane raise must not hide drift toward the new ceiling — so record what it bought (#4048)."""
    pytest.importorskip("pytest_timeout")
    (tmp_path / "test_ceilings.py").write_text(_CEILING_SUITE, encoding="utf-8")
    stats_out = tmp_path / "shard-stats.1.json"

    env = _clean_env()
    env["PYTHONPATH"] = str(_REPO_ROOT)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_ceilings.py",
            "-p",
            "scripts.ci.shard_stats_plugin",
            "-o",
            "timeout=300",
            f"--shard-stats-out={stats_out}",
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert stats_out.exists(), completed.stdout + completed.stderr
    payload = json.loads(stats_out.read_text(encoding="utf-8"))
    recorded = {entry["node_id"]: entry for entry in payload["slowest_against_ceiling"]}
    assert recorded, "the headroom record must survive a passing session"
    assert recorded["test_ceilings.py::test_under_the_lane_ceiling"]["ceiling"] == 300
    assert recorded["test_ceilings.py::test_states_its_own_ceiling"]["ceiling"] == 900, (
        "a `@pytest.mark.timeout` beats the lane value in `pytest_timeout._get_item_settings`, "
        "so the record must judge that test against its own stated ceiling"
    )
    assert all(entry["seconds"] >= 0 for entry in recorded.values())


@pytest.mark.integration
def test_the_partition_record_survives_a_session_that_dies_mid_run(tmp_path: Path) -> None:
    """The completeness check reads this file; a hard crash must not cost it the counts it needs.

    ``os._exit`` rather than an exception on purpose — pytest absorbs an exception and
    still runs ``pytest_sessionfinish``, so a session-finish-only write would pass this
    vacuously. The counts have to be on disk from collection time.
    """
    (tmp_path / "test_toy.py").write_text(
        _TOY_SUITE + "\n\ndef test_boom():\n    import os\n\n    os._exit(3)\n", encoding="utf-8"
    )
    stats_out = tmp_path / "shard-stats.1.json"

    env = _clean_env()
    env["PYTHONPATH"] = str(_REPO_ROOT)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_toy.py",
            "-p",
            "scripts.ci.shard_stats_plugin",
            f"--shard-stats-out={stats_out}",
            "-q",
            "-x",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(stats_out.read_text(encoding="utf-8"))
    assert payload["total_collected"] == 11


def _clean_env() -> dict[str, str]:
    import os  # noqa: PLC0415 — local so the module stays a thin plugin test

    # Strip GIT_* so an inline pre-commit ``pytest`` run's exported git env can't
    # leak into the toy subprocess (AGENTS.md § Test-Writing Doctrine).
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
