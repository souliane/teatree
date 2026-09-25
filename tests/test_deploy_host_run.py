# test-path: cross-cutting — drives deploy/t3 as a real bash program (no src mirror).
"""The generic host hop for an overlay tool leaf: container resolves, wrapper carries out.

An overlay tool whose work needs the operator's own machine — a binary, a credential, a
loopback port, a browser — cannot finish inside the container. ``t3 admin`` and ``t3 peer``
already take this hop; both are keyed on hard-coded CORE verbs, so an overlay leaf had no
way to reach it and the tool simply failed in a venue that could not do its job.

The wrapper is exercised for real — the genuine ``deploy/t3`` copied into a fork layout and
run against a ``docker`` stub. Docker is the unstoppable external.
"""

import dataclasses
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.core.host_hop.host_run import PLAN_DIR, PLAN_NAME, RUNNER_NAME

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
SYSTEM_PATH = os.defpath.strip(os.pathsep)

pytestmark = pytest.mark.skipif(
    shutil.which("bash", path=SYSTEM_PATH) is None,
    reason="needs a system bash (present on macOS, in the deploy image, and in CI)",
)


@dataclasses.dataclass(frozen=True, slots=True)
class Stubs:
    """What the stubbed world answers on one run.

    ``running`` is the only field that decides which route the wrapper takes; the other
    three are what each half of the hop reports back once it is on one.
    """

    running: bool = True
    stages: bool = True
    container_rc: int = 0
    runner_rc: int = 0


#: The default world: a running stack whose container half stages, and both halves green.
DEFAULT_STUBS = Stubs()


def _docker_stub(*, plan_dir: Path, witness: Path, stubs: Stubs) -> str:
    """A ``docker`` stub whose container half plays the hop -- on either route.

    It writes the runner and the plan exactly where the CLI would, so what is under test
    is the wrapper's own read of them. The staged runner records its argv and exits a
    code the test chooses, which is how the rc contract is observed.

    ``exec`` (a running stack) and ``run`` (the one-off) stage IDENTICALLY, because the
    container half cannot tell them apart: ``running_in_container()`` is true in both, so
    ``host_run.dispatch`` stages and returns 0 in both. ``running=False`` empties the
    ``ps`` table, which is the only thing that decides which route the wrapper takes.
    """
    staging = (
        f"mkdir -p {plan_dir}\n"
        f"printf '%s\\n' '#!/usr/bin/env bash' 'printf \"%s\\\\n\" \"$@\" >>{witness}' "
        f"'exit ${{RUNNER_RC:-0}}' >{plan_dir}/{RUNNER_NAME}\n"
        f"chmod 700 {plan_dir}/{RUNNER_NAME}\n"
        f"printf 'action=up\\n' >{plan_dir}/{PLAN_NAME}\n"
        if stubs.stages
        else "true\n"
    )
    ps_table = "echo 'teatree-worker-1 teatree-worker False'" if stubs.running else "true"
    return f"""#!/usr/bin/env bash
if [ "$1" = inspect ]; then printf '%s' "bind {os.environ.get("PWD", "/")}"; exit 0; fi
if [ "$1" = version ]; then echo 99; exit 0; fi
for arg in "$@"; do
    if [ "$arg" = ps ]; then {ps_table}; exit 0; fi
    if [ "$arg" = config ]; then exit 0; fi
    if [ "$arg" = exec ] || [ "$arg" = run ]; then
{staging}        exit {stubs.container_rc}
    fi
done
exit 0
"""


def _fork_checkout(root: Path) -> Path:
    entry = root / "vendor" / "teatree" / "deploy" / "t3"
    entry.parent.mkdir(parents=True)
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    (root / "pyproject.toml").write_text("[project]\nname = 'fork'\n", encoding="utf-8")
    shutil.copy2(WRAPPER, entry.parent / "docker-compose.yml")
    return entry


def _run(
    tmp_path: Path, argv: list[str], stubs: Stubs = DEFAULT_STUBS
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "home"
    plan_dir = home / ".local" / "share" / "teatree" / PLAN_DIR
    witness = tmp_path / "runner-argv"

    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_text(_docker_stub(plan_dir=plan_dir, witness=witness, stubs=stubs), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    entry = _fork_checkout(tmp_path / "fork")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{SYSTEM_PATH}"
    env["TEATREE_HOST_HOME"] = str(home)
    env["RUNNER_RC"] = str(stubs.runner_rc)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)

    completed = subprocess.run([str(entry), *argv], capture_output=True, text=True, check=False, env=env, cwd=elsewhere)
    return completed, witness


class TestAnOverlayToolLeafTakesTheHostHop:
    def test_the_staged_runner_runs_with_the_plan_directory(self, tmp_path: Path) -> None:
        completed, witness = _run(tmp_path, ["acme", "tool", "thing"])

        assert witness.is_file(), f"the runner never ran: {completed.stderr}"
        assert witness.read_text(encoding="utf-8").strip() == str(
            tmp_path / "home" / ".local" / "share" / "teatree" / PLAN_DIR
        )

    def test_the_runners_exit_code_is_the_wrappers(self, tmp_path: Path) -> None:
        completed, _ = _run(tmp_path, ["acme", "tool", "thing"], Stubs(runner_rc=4))

        assert completed.returncode == 4, completed.stderr

    def test_a_tool_that_finished_itself_leaves_no_plan_and_exits_zero(self, tmp_path: Path) -> None:
        completed, witness = _run(tmp_path, ["acme", "tool", "thing"], Stubs(stages=False))

        assert completed.returncode == 0, completed.stderr
        assert not witness.exists(), "no plan means the tool finished in the container"

    def test_a_failed_container_half_never_runs_the_runner(self, tmp_path: Path) -> None:
        completed, witness = _run(tmp_path, ["acme", "tool", "thing"], Stubs(container_rc=2))

        assert completed.returncode == 2, completed.stderr
        assert not witness.exists(), "a refusal in the container must not be carried out on the host"


class TestTheHopIsScopedToToolLeaves:
    def test_a_non_tool_verb_ignores_a_stale_plan(self, tmp_path: Path) -> None:
        # `tool` is the SECOND word for an overlay leaf. Any other verb finishes in the
        # container, so a plan lying around from an earlier run must not be carried out.
        completed, witness = _run(tmp_path, ["acme", "worktree", "status"])

        assert not witness.exists(), completed.stderr

    def test_core_top_level_tool_is_not_an_overlay_leaf(self, tmp_path: Path) -> None:
        # Core's own `t3 tool <x>` has `tool` FIRST; it has no host half.
        completed, witness = _run(tmp_path, ["tool", "repo-mode"])

        assert not witness.exists(), completed.stderr


class TestTheHopDoesNotDependOnTheStackBeingUp:
    """A stopped stack stages the same plan — and the wrapper used to exec away from it.

    ``running_in_container()`` is true in the one-off ``compose run`` exactly as it is in a
    ``compose exec``, so the container half stages either way and ``dispatch`` returns 0
    either way. Gating the hop on a running service therefore did not make the tool fall
    back to doing the work itself: it exited 0 having staged a plan nobody would ever read.
    A caller cannot tell that apart from success, which makes it worse than the
    unreachable command the hop was introduced to fix.
    """

    def test_the_stopped_route_is_genuinely_the_one_off_container(self, tmp_path: Path) -> None:
        # Anti-vacuity: without this, a stub that quietly still reported a running worker
        # would make every assertion below a second copy of the running-stack tests.
        completed, _ = _run(tmp_path, ["acme", "tool", "thing"], Stubs(running=False))

        assert "dispatching in a one-off container" in completed.stderr, completed.stderr

    def test_the_staged_runner_runs_when_the_stack_is_stopped(self, tmp_path: Path) -> None:
        completed, witness = _run(tmp_path, ["acme", "tool", "thing"], Stubs(running=False))

        assert witness.is_file(), f"the plan was staged and never carried out: {completed.stderr}"
        assert witness.read_text(encoding="utf-8").strip() == str(
            tmp_path / "home" / ".local" / "share" / "teatree" / PLAN_DIR
        )

    def test_the_runners_exit_code_is_the_wrappers_when_the_stack_is_stopped(self, tmp_path: Path) -> None:
        # rc 0 must MEAN the host half ran, so a failing one must not read as success.
        completed, _ = _run(tmp_path, ["acme", "tool", "thing"], Stubs(running=False, runner_rc=4))

        assert completed.returncode == 4, completed.stderr

    def test_a_stopped_stack_tool_that_finished_itself_still_exits_zero(self, tmp_path: Path) -> None:
        # The empty-plan contract is unchanged by the route: no plan means the tool
        # genuinely finished in the container, which is a real success.
        completed, witness = _run(tmp_path, ["acme", "tool", "thing"], Stubs(running=False, stages=False))

        assert completed.returncode == 0, completed.stderr
        assert not witness.exists()


class TestTheHopIsNotGatedOnAStackProbe:
    def test_no_line_gates_the_host_run_hop_on_a_running_service(self) -> None:
        """The update-recovery half of the same defect, refused at the source.

        The update-wait loop exists to notice a route coming BACK, so it can repopulate
        the running service after an earlier probe found none. A hop decided by
        ``first_running_service`` at the top is therefore decided on a reading the loop
        is about to invalidate, and the recovered route execs straight past the plan.
        Deciding the hop at each terminal dispatch point instead is what makes that
        unrepresentable — there is no third route — so this refuses the pairing that
        would reopen it. No stub can reach the loop itself: it is gated on
        ``deploy_lock_held``, which reads ``/proc/locks`` and is unreachable off Linux.
        """
        gated = [
            line.strip()
            for line in WRAPPER.read_text(encoding="utf-8").splitlines()
            if "wants_host_run" in line and "first_running_service" in line
        ]

        assert not gated, f"the host-run hop is gated on a stack probe again: {gated}"
