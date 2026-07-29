"""The order-dependence (shuffle) lane audits a curated, order-safe set (#3160 CI-4).

The lane was scoped to ``tests/teatree_loop/`` alone. It is widened to every
additional directory empirically verified order-safe under all four matrixed seeds,
both standalone and shuffled together in one process. This pins the widened set so a
future edit that silently narrows the lane back to the loop dir turns red here, and
keeps the original loop dir (the #2359 Class B reproducer home) in scope.

``TestShuffleRunnerCannotReportAFalseGreen`` pins the second half: the LOCAL twin
``dev/test-shuffle.sh`` and the guards that stop a missing plugin group masquerading as
success. ``pytest-randomly`` lives in the non-default ``shuffle`` group, so a hand-rolled
``uv run pytest -p randomly ... | tail -5`` on a plain ``dev`` env raises
``ImportError: Error importing plugin "randomly"`` while the PIPELINE exits 0 — a shell
pipeline reports its LAST stage's status, so a lane that collected nothing reads green to
anything gating on the exit code (measured: bare pytest exits 1, the piped run exits 0).
"""

import os
import shlex
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SHUFFLE_SCRIPT = _REPO_ROOT / "dev" / "test-shuffle.sh"

# The directories empirically confirmed order-safe under shuffle (seeds 7/1/13/100),
# both standalone and shuffled together as one -n0 process.
_WIDENED_DIRS = (
    "tests/teatree_loop/",
    "tests/config/",
    "tests/teatree_config/",
    "tests/teatree_utils/",
    "tests/utils/",
    "tests/teatree_quality/",
    "tests/messaging/",
    "tests/cli_doctor/",
    "tests/conformance/",
    "tests/teatree_hooks/",
)


def _shuffle_run() -> str:
    jobs = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
    steps = [s for s in jobs["test-shuffle"]["steps"] if isinstance(s, dict)]
    run_steps = [str(s.get("run", "")) for s in steps if "randomly" in str(s.get("run", ""))]
    assert run_steps, "test-shuffle must have a step that runs pytest under -p randomly."
    return run_steps[0]


class TestShuffleLaneScope:
    def test_still_includes_the_loop_reproducer_dir(self) -> None:
        assert "tests/teatree_loop/" in _shuffle_run(), (
            "The shuffle lane must keep tests/teatree_loop/ — the #2359 Class B "
            "order-dependence reproducer lives there."
        )

    def test_lane_is_widened_beyond_the_loop_dir(self) -> None:
        run = _shuffle_run()
        for directory in _WIDENED_DIRS:
            assert directory in run, (
                f"The shuffle lane must audit {directory} — it was confirmed order-safe under "
                "all four matrixed seeds (CI-4). A missing dir silently narrows the audit."
            )

    def test_runs_serially_under_shuffle(self) -> None:
        # -n0 (serial) is load-bearing: xdist would isolate a polluter from its victim
        # across workers, defeating the whole order-dependence audit.
        run = _shuffle_run()
        assert "-n0" in run, "The shuffle lane must run serially (-n0) so one in-process order is exercised."
        assert "-p randomly" in run, "The shuffle lane must load pytest-randomly."

    def test_ci_lane_installs_the_shuffle_group(self) -> None:
        # pytest-randomly is NOT in the default `dev` group, so the lane is only ever
        # real if it installs `--group shuffle` first. Without it `-p randomly` raises
        # ImportError — loud on its own, but silently exit-0 the moment the run is piped.
        jobs = cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])
        runs = " ".join(str(s.get("run", "")) for s in jobs["test-shuffle"]["steps"] if isinstance(s, dict))
        assert "--group shuffle" in runs, (
            "The test-shuffle lane must `uv sync --group shuffle` — pytest-randomly is out of the "
            "default dev group, so without it the lane loads no plugin and audits no order."
        )


def _shuffle_script() -> str:
    assert _SHUFFLE_SCRIPT.is_file(), (
        "dev/test-shuffle.sh is missing — the shuffle lane must have a LOCAL runner. Without one, "
        "agents hand-roll `uv run pytest -p randomly ... | tail`, whose pipeline erases the "
        "plugin-import failure into exit 0."
    )
    return _SHUFFLE_SCRIPT.read_text(encoding="utf-8")


def _code_lines(body: str) -> list[str]:
    """The EXECUTABLE lines of a shell script — continuations joined, comments dropped.

    Every assertion below reads this rather than the raw text: this file's own prose
    explains each guard by naming the very token it asserts (``pipefail``, ``-n0``,
    ``required_plugins=…``), so a raw substring check would stay green after the guard
    was deleted from the code — a vacuous test. Only what the shell would RUN counts.
    """
    joined = body.replace("\\\n", " ")
    return [line for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _shuffle_script_code() -> str:
    return "\n".join(_code_lines(_shuffle_script()))


def _pytest_command_lines(body: str) -> list[str]:
    """Every executable line of *body* that INVOKES pytest.

    ``echo`` lines are excluded: the runner's own diagnostics quote the flags they
    explain (``'pytest -p randomly' exits non-zero``), and a blob-level substring check
    would let a deleted flag stay green on the strength of its own error message.
    """
    return [line for line in _code_lines(body) if "pytest" in line and not line.lstrip().startswith("echo ")]


def _shuffle_pytest_invocation() -> str:
    lines = [line for line in _pytest_command_lines(_shuffle_script()) if " pytest " in f" {line} "]
    assert lines, "dev/test-shuffle.sh must actually invoke pytest."
    return "\n".join(lines)


def _shuffle_uv_commands() -> str:
    """The runner's executable ``uv`` commands — again excluding its own ``echo`` prose."""
    return "\n".join(
        line for line in _code_lines(_shuffle_script()) if "uv " in line and not line.lstrip().startswith("echo ")
    )


class TestShuffleRunnerCannotReportAFalseGreen:
    """The local twin exists, matches CI's scope, and cannot swallow a failing exit code.

    Each assertion pins one of the three guards in ``dev/test-shuffle.sh``: no pipeline
    can erase an exit status, the required group is asserted BEFORE the run, and pytest
    itself re-asserts the plugin in-session. Removing any one of them re-opens the
    observed failure — a shuffle run that "passed" while importing no plugin at all.
    """

    def test_local_runner_exists_and_is_executable(self) -> None:
        _shuffle_script()
        assert os.access(_SHUFFLE_SCRIPT, os.X_OK), "dev/test-shuffle.sh must be executable."

    def test_runner_enables_pipefail(self) -> None:
        # THE observed defect: a pipeline reports its LAST stage's status, so
        # `pytest ... | tail` exits 0 on a pytest that exited 1. `pipefail` is the
        # shell-level guard; without it any pipe added later re-opens the hole.
        assert "pipefail" in _shuffle_script_code(), (
            "dev/test-shuffle.sh must `set -euo pipefail` — without it a piped stage silently "
            "rewrites a failing pytest run's exit code to 0."
        )

    def test_runner_never_pipes_a_pytest_invocation(self) -> None:
        # Belt-and-braces over pipefail: the runner must not pipe pytest at all, so its
        # status reaches the caller untouched even if `pipefail` is ever dropped.
        piped = [line for line in _pytest_command_lines(_shuffle_script()) if "|" in shlex.split(line, comments=True)]
        assert not piped, f"dev/test-shuffle.sh pipes a pytest invocation — that is the exact exit-0 mask: {piped}"

    def test_runner_installs_and_then_preflights_the_shuffle_group(self) -> None:
        assert "--group shuffle" in _shuffle_uv_commands(), (
            "dev/test-shuffle.sh must install the non-default `shuffle` group."
        )
        assert "import pytest_randomly" in _shuffle_script_code(), (
            "dev/test-shuffle.sh must PREFLIGHT `import pytest_randomly` and fail loud — an absent "
            "group must be reported as an absent group, never as a passing lane."
        )

    def test_runner_requires_the_plugin_in_session(self) -> None:
        # pytest's own assertion, so an invocation that somehow reaches pytest without
        # the plugin errors in-session instead of quietly running unshuffled.
        assert "required_plugins=pytest-randomly" in _shuffle_pytest_invocation(), (
            "dev/test-shuffle.sh must pass `-o required_plugins=pytest-randomly` so a degraded "
            "environment cannot produce an UNSHUFFLED green."
        )

    def test_runner_runs_serially_under_the_randomly_plugin(self) -> None:
        invocation = _shuffle_pytest_invocation()
        assert "-n0" in invocation, "dev/test-shuffle.sh must run serially (-n0), like CI's lane."
        assert "-p randomly" in invocation, "dev/test-shuffle.sh must load pytest-randomly."

    def test_runner_audits_exactly_the_ci_lane_directories(self) -> None:
        # Parity in BOTH directions: a local twin that audits fewer dirs gives a false
        # local green, and one that audits more asserts order-safety CI never verified.
        code = _shuffle_script_code()
        local = {directory for directory in _WIDENED_DIRS if directory in code}
        assert local == set(_WIDENED_DIRS), (
            f"dev/test-shuffle.sh must audit exactly CI's curated set — missing: {sorted(set(_WIDENED_DIRS) - local)}"
        )
        ci_dirs = {token for token in _shuffle_run().split() if token.startswith("tests/")}
        script_dirs = {token for token in code.split() if token.startswith("tests/")}
        assert script_dirs == ci_dirs, (
            f"local runner and CI lane disagree on scope — local-only: {sorted(script_dirs - ci_dirs)}, "
            f"CI-only: {sorted(ci_dirs - script_dirs)}"
        )
