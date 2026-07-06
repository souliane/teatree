"""Shared abstract harness for real-provisioning integration tests.

A subclass declares *which* overlay, *how many* concurrent worktrees, the
*time cap*, the *DB-touching test* to run per worktree, and the *readiness
probes* that prove the started runtime serves. The concrete machinery here
provisions a real git worktree, runs the DB test as its own ``pytest``
subprocess by node id (Django is **not** booted inside it), starts the
server(s), and asserts reachability through
:func:`teatree.core.worktree.readiness.run_probes`.

Teardown finalizers are registered per worktree *at creation*, before the
start attempt, so a half-started worktree is still torn down.
"""

import abc
import os
import socket
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.overlay import OverlayBase
from teatree.core.worktree.readiness import Probe, ProbeResult, run_probes
from teatree.utils import git
from teatree.utils.run import spawn


def git_clean_env() -> dict[str, str]:
    """Process env with every ``GIT_*`` override stripped.

    The pre-commit ``pytest`` hook runs under an outer ``git commit`` that
    exports ``GIT_DIR`` / ``GIT_INDEX_FILE`` / ``GIT_WORK_TREE``. A child
    ``git`` (or ``uv run pytest`` that shells out to git) inherits these and
    hijacks itself onto the outer repo's index instead of the tmp worktree.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def free_tcp_port() -> int:
    """Bind to port 0, read the OS-assigned port, release it.

    The brief release-then-reuse race is acceptable for a test harness — the
    server claims the port within milliseconds and a collision merely fails
    the boot probe rather than corrupting state.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ProvisionedWorktree:
    """A real git worktree plus the runtime state a probe needs.

    ``extra['ports']`` mirrors what ``WorktreeStartRunner`` stores on a real
    ``Worktree`` model, so an overlay's ``get_readiness_probes`` consumes it
    unchanged. ``servers`` are the spawned processes the teardown reaps.
    """

    path: Path
    repo: str
    ports: dict[str, int] = field(default_factory=dict)
    servers: list[subprocess.Popen[str]] = field(default_factory=list)

    @property
    def extra(self) -> dict[str, object]:
        return {"worktree_path": str(self.path), "ports": dict(self.ports)}


class ProvisioningIntegrationBase(abc.ABC):
    """Provision → DB-test → start → assert-reachable, on real artifacts."""

    @abc.abstractmethod
    def overlay(self) -> OverlayBase: ...

    @abc.abstractmethod
    def workspace_repos(self) -> list[str]: ...

    @abc.abstractmethod
    def concurrency(self) -> int: ...

    @abc.abstractmethod
    def db_touch_test_command(self, wt: ProvisionedWorktree) -> Sequence[str]:
        """A ``pytest <nodeid>`` argv that touches the DB.

        It must NOT start Django/a server — DB reachability is asserted on
        its own, before the long-lived server process is spawned.
        """

    def db_test_timeout_seconds(self) -> float:
        """Hard timeout for the DB-test subprocess.

        A slow CI box may take longer to spin up a fresh interpreter
        without the harness being broken, so the subprocess gets generous
        headroom while ``@pytest.mark.timeout`` on the test class remains
        the true hang guard.
        """
        return 60.0

    @abc.abstractmethod
    def readiness_probes(self, wt: ProvisionedWorktree) -> list[Probe]:
        """HTTP probes proving the started runtime actually serves."""

    @abc.abstractmethod
    def _start(self, wt: ProvisionedWorktree) -> None:
        """Start the server(s) for *wt*, recording each into ``wt.servers``."""

    def _provision(self, root: Path, repo: str) -> ProvisionedWorktree:
        wt_path = root / repo
        wt_path.mkdir(parents=True, exist_ok=True)
        git.run_strict(repo=str(wt_path), args=["init", "-q", "-b", "main"])
        return ProvisionedWorktree(path=wt_path, repo=repo)

    def _run_db_test(self, wt: ProvisionedWorktree) -> None:
        cmd = list(self.db_touch_test_command(wt))
        result = subprocess.run(
            cmd,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=self.db_test_timeout_seconds(),
            check=False,
        )
        assert result.returncode == 0, (
            f"DB-touching test failed ({' '.join(cmd)}):\n"
            f"--- stdout ---\n{result.stdout[-2000:]}\n"
            f"--- stderr ---\n{result.stderr[-2000:]}"
        )

    def _assert_reachable(self, wt: ProvisionedWorktree) -> None:
        probes = self.readiness_probes(wt)
        assert probes, f"no readiness probes declared for {wt.repo}"
        results: list[ProbeResult] = run_probes(probes)
        failures = [r for r in results if not r.passed]
        assert not failures, "; ".join(r.format() for r in failures)

    def _teardown(self, wt: ProvisionedWorktree) -> None:
        for server in wt.servers:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)

    def run_one_worktree(self, wt: ProvisionedWorktree) -> None:
        self._run_db_test(wt)
        self._start(wt)
        self._assert_reachable(wt)

    def assert_concurrently_alive(self, worktrees: list[ProvisionedWorktree]) -> None:
        """All started servers are alive at the SAME instant — load-free.

        The genuine-concurrency / non-serialization invariant, asserted on
        the work actually done rather than on elapsed wall-clock (which is
        non-deterministic under load and so cannot gate a pre-push run).
        After :meth:`run_concurrently` every worktree has been started and
        proven reachable and no teardown has run yet, so every server
        process must still be live: a serialized run that tore one down
        before starting the next, or a crashed/hung server, leaves a dead
        process here. ``@pytest.mark.timeout`` on the test class remains the
        hang guard.
        """
        servers = [(wt.repo, server) for wt in worktrees for server in wt.servers]
        assert servers, "no servers were started"
        dead = [repo for repo, server in servers if server.poll() is not None]
        assert not dead, f"servers not concurrently alive (exited early): {dead}"

    def _spawn_server(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        env = git_clean_env()
        env.update(env_overrides or {})
        return spawn(
            cmd,
            env=env,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def provision_all(
        self,
        root: Path,
        register_finalizer: Callable[[Callable[[], None]], None],
    ) -> list[ProvisionedWorktree]:
        """Provision every workspace repo, registering teardown BEFORE start.

        The finalizer is wired the instant the worktree exists — so a worktree
        that later fails to start is still reaped.
        """
        worktrees: list[ProvisionedWorktree] = []
        for repo in self.workspace_repos():
            wt = self._provision(root, repo)
            register_finalizer(lambda wt=wt: self._teardown(wt))
            worktrees.append(wt)
        return worktrees

    def run_concurrently(self, worktrees: list[ProvisionedWorktree]) -> None:
        workers = min(self.concurrency(), len(worktrees)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self.run_one_worktree, worktrees))
