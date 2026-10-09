"""The raised per-test ceiling belongs to the contended lane only (#4048).

Twelve shards each running ``-n auto`` on one runner cost a test roughly 3x what
it costs alone (#4035, measured: 20.15s locally against a ceiling it then
exceeded under the matrix). The ceiling that catches a hang in seconds locally
therefore catches ordinary contention in CI, and reds a PR whose diff never
touched the test.

Raising the ini value would fix that lane and lose the tight local one — the ini
is inherited by ``dev/ci-parity-fast.sh``, ``dev/ci-shard.sh`` and every bare
``uv run pytest``, which are exactly where a fast hang signal is worth most. So
the raise is a lane-scoped ``-o timeout=`` on the sharded job, and these
assertions keep it there: the ini stays tight, the serial shuffle lane (which has
no contention to pay for) inherits it untouched, and only the shard job overrides.
"""

import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The tight local ceiling: high enough for the slowest honest test, low enough
# that a hang is a fast signal rather than a coffee break.
MAX_INI_TIMEOUT_SECONDS = 60


def _jobs() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]


def _run_steps(job: dict) -> list[str]:
    return [str(step["run"]) for step in job.get("steps", []) if "run" in step]


def _pytest_steps(job: dict) -> list[str]:
    return [run for run in _run_steps(job) if "pytest" in run]


def _lane_timeout(run: str) -> int | None:
    tokens = run.split()
    for index, token in enumerate(tokens):
        if token == "-o" and index + 1 < len(tokens) and tokens[index + 1].startswith("timeout="):
            return int(tokens[index + 1].removeprefix("timeout="))
        if token.startswith("-otimeout="):
            return int(token.removeprefix("-otimeout="))
    return None


class TestTheIniCeilingStaysTight:
    def test_pyproject_keeps_the_fast_local_hang_signal(self) -> None:
        config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        ini_timeout = config["tool"]["pytest"]["ini_options"]["timeout"]
        assert ini_timeout <= MAX_INI_TIMEOUT_SECONDS, (
            "The sharded CI lane's contention is paid for by its own `-o timeout=` override, not by "
            "raising the ini every local run inherits — a raised ini turns a hang into a coffee break "
            "in `dev/ci-parity-fast.sh`, `dev/ci-shard.sh` and every bare `uv run pytest`."
        )


class TestOnlyTheContendedLaneRaisesIt:
    def test_the_shard_matrix_states_its_own_ceiling(self) -> None:
        steps = _pytest_steps(_jobs()["test-shard"])
        assert steps, "the shard job must run pytest"
        raised = [_lane_timeout(run) for run in steps]
        assert all(seconds is not None for seconds in raised), (
            "The 12-way shard matrix runs contended, so it must state its own `-o timeout=` rather "
            "than inherit the ini ceiling sized for an idle tree (#4035)."
        )
        assert all(seconds > MAX_INI_TIMEOUT_SECONDS for seconds in raised if seconds is not None)

    def test_the_serial_shuffle_lane_keeps_the_tight_one(self) -> None:
        """One seed at a time at ``-n0`` — no contention to pay for, so no raise to inherit."""
        steps = _pytest_steps(_jobs()["test-shuffle"])
        assert steps, "the shuffle job must run pytest, or this asserts nothing"
        for run in steps:
            assert _lane_timeout(run) is None, (
                "The shuffle lane runs serially on its own runner; a raised ceiling there only "
                "delays an order-dependence hang it exists to surface."
            )

    def test_no_other_job_quietly_raises_it(self) -> None:
        raisers = {
            name for name, job in _jobs().items() if any(_lane_timeout(run) is not None for run in _pytest_steps(job))
        }
        assert raisers == {"test-shard"}, (
            f"Only the contended shard matrix may override the per-test ceiling; found {sorted(raisers)}. "
            "A raise elsewhere is a hang signal traded away with no contention to pay for it."
        )
