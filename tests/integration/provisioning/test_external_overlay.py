"""Real-provisioning integration test for a registered external overlay.

The companion to ``test_teatree_self.py``: where that test pins the bundled
overlay, this one exercises whatever *external* full-stack overlay is
installed (an overlay that owns multiple repos — a backend, a frontend, a
microservice) entirely through the :class:`~teatree.core.overlay.OverlayBase`
ABC. It stays overlay-agnostic on purpose — it names no overlay, no client,
no repo — so the same harness validates any third-party overlay's real
provisioning and the public repo carries no overlay-specific identifiers
(BLUEPRINT § 1).

Concurrency 1: one workspace, one fast DB-touching test, then every service
the overlay declares is started and asserted reachable through
``overlay.get_readiness_probes``.

Heavy and environment-bound, so it is skipped unless ALL hold: ``docker``
is on ``PATH`` (an external full-stack overlay starts its services via
compose); exactly one external (non-bundled) overlay is registered,
instantiable, declares more than one on-disk workspace repo; and
``T3_EXTERNAL_OVERLAY_INTEGRATION=1`` is set (explicit opt-in).

The skip lives here, not in ``src/``, so default CI stays green and the 93%
coverage gate is untouched.
"""

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from teatree.config import discover_overlays, load_config
from teatree.core.overlay import OverlayBase, RunCommand
from teatree.core.overlay_loader import get_overlay
from teatree.core.worktree.readiness import Probe
from teatree.core.worktree.worktree_env import compose_project
from teatree.utils.ports import get_worktree_ports

from ._base import ProvisionedWorktree, ProvisioningIntegrationBase

_BUNDLED_OVERLAY = "t3-teatree"
_MULTI_REPO_THRESHOLD = 1


@dataclass
class _ProbeWorktree:
    """Minimal ``Worktree`` stand-in for the overlay's ABC hooks.

    Overlays read only ``repo_path``, ``worktree_path`` (via ``extra``), and
    ``extra['ports']`` from the worktree they're handed; a real model row
    would drag in the FSM and DB lifecycle this harness does not exercise.
    """

    repo_path: str
    extra: dict[str, object] = field(default_factory=dict)
    ticket: object | None = None

    @property
    def worktree_path(self) -> str:
        return str(self.extra.get("worktree_path", ""))


def _external_overlay_name() -> str:
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    workspace_dir = load_config().user.workspace_dir.expanduser()
    instantiable = set(get_all_overlays())
    candidates: list[str] = []
    for entry in discover_overlays():
        if entry.name == _BUNDLED_OVERLAY or entry.name not in instantiable:
            continue
        if entry.project_path is None or not (workspace_dir / entry.project_path.name).is_dir():
            continue
        candidates.append(entry.name)
    return candidates[0] if len(candidates) == 1 else ""


def _resolvable_repos(overlay: OverlayBase) -> list[str]:
    workspace_dir = load_config().user.workspace_dir.expanduser()
    return [repo for repo in overlay.get_workspace_repos() if (workspace_dir / Path(repo).name).is_dir()]


def _skip_reason() -> str:
    if os.environ.get("T3_EXTERNAL_OVERLAY_INTEGRATION") != "1":
        return "set T3_EXTERNAL_OVERLAY_INTEGRATION=1 to run the external-overlay provisioning integration test"
    if shutil.which("docker") is None:
        return "docker not on PATH"
    name = _external_overlay_name()
    if not name:
        return "no single instantiable external overlay with an on-disk project is registered"
    overlay = get_overlay(name)
    if len(_resolvable_repos(overlay)) <= _MULTI_REPO_THRESHOLD:
        return f"external overlay {name!r} has fewer than two repos resolvable on disk"
    return ""


@pytest.mark.integration
@pytest.mark.timeout(150)
@pytest.mark.skipif(bool(_skip_reason()), reason=_skip_reason() or "external-overlay prerequisites met")
class TestExternalOverlayProvisioning(ProvisioningIntegrationBase):
    def overlay(self) -> OverlayBase:
        return get_overlay(_external_overlay_name())

    def workspace_repos(self) -> list[str]:
        return ["workspace"]

    def concurrency(self) -> int:
        return 1

    def _backend_root(self) -> Path:
        workspace_dir = load_config().user.workspace_dir.expanduser()
        return workspace_dir / Path(_resolvable_repos(self.overlay())[0]).name

    def db_touch_test_command(self, wt: ProvisionedWorktree) -> Sequence[str]:
        backend = self._backend_root()
        return [
            "docker",
            "compose",
            "-f",
            str(backend / "docker-compose.yml"),
            "run",
            "--rm",
            "web",
            "python",
            "manage.py",
            "migrate",
            "--check",
        ]

    def _probe_worktree(self, repo: str, ports: dict[str, int]) -> _ProbeWorktree:
        workspace_dir = load_config().user.workspace_dir.expanduser()
        return _ProbeWorktree(
            repo_path=repo,
            extra={"worktree_path": str(workspace_dir), "ports": dict(ports)},
        )

    def readiness_probes(self, wt: ProvisionedWorktree) -> list[Probe]:
        overlay = self.overlay()
        probes: list[Probe] = []
        for repo in _resolvable_repos(overlay):
            probes.extend(overlay.get_readiness_probes(self._probe_worktree(repo, wt.ports)))
        return probes

    def _start(self, wt: ProvisionedWorktree) -> None:
        overlay = self.overlay()
        workspace_dir = load_config().user.workspace_dir.expanduser()
        commands = overlay.get_run_commands(self._probe_worktree(_resolvable_repos(overlay)[0], wt.ports))
        for command in commands.values():
            if isinstance(command, RunCommand):
                args = [str(a) for a in command.args]
                cwd = command.cwd or workspace_dir
            else:
                args = [str(a) for a in command]
                cwd = workspace_dir
            wt.servers.append(self._spawn_server(args, cwd=Path(cwd)))
        backend = self._backend_root()
        project = compose_project(_ProbeWorktree(repo_path=backend.name))
        wt.ports.update(get_worktree_ports(project, compose_file=str(backend / "docker-compose.yml")))

    def test_workspace_provisions_serves_reachable(
        self,
        provisioning_root: tuple[Path, Callable[[Callable[[], None]], None]],
    ) -> None:
        root, register_finalizer = provisioning_root
        worktrees = self.provision_all(root, register_finalizer)
        assert len(worktrees) == 1

        self.run_concurrently(worktrees)

        self.assert_concurrently_alive(worktrees)
