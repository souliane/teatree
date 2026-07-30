"""Integration guard for the shard-stats pytest plugin.

Runs a real ``pytest`` subprocess over a toy suite with ``pytest-split``'s
``--splits 2 --group 1`` plus ``scripts/ci/shard_stats_plugin.py`` and asserts the
emitted JSON records the FULL collection as ``total_collected`` and only the
group's slice as ``selected`` — the exact contract
``scripts/ci/check_shard_completeness.py`` relies on. Skipped where pytest-split
is not installed (the plugin ships in the opt-in ``shard`` dependency group,
mirroring the shuffle-lane isolation).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pytest_split")

_REPO_ROOT = Path(__file__).resolve().parents[1]

_TOY_SUITE = """
import pytest


@pytest.mark.parametrize("i", range(10))
def test_toy(i):
    assert i >= 0
"""


@pytest.mark.integration
def test_plugin_records_total_and_group_slice(tmp_path: Path) -> None:
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


def _clean_env() -> dict[str, str]:
    import os  # noqa: PLC0415 — local so the module stays a thin plugin test

    # Strip GIT_* so an inline pre-commit ``pytest`` run's exported git env can't
    # leak into the toy subprocess (AGENTS.md § Test-Writing Doctrine).
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
