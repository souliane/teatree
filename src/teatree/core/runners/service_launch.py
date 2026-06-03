from typing import TYPE_CHECKING

from teatree.core.overlay_loader import get_overlay_for_worktree
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.step_runner import run_provision_steps
from teatree.types import RunCommand
from teatree.utils.run import run_streamed

if TYPE_CHECKING:
    from teatree.core.models import Worktree
    from teatree.core.overlay import OverlayBase, ProvisionStep


class ServiceLauncher(RunnerBase):
    """Runs a worktree host service, always after its pre-run steps.

    The only supported way to run a service. ``get_run_commands`` is reachable
    only through here, so a caller cannot run a service without its
    prerequisites — the drift that let ``run build-frontend`` skip
    ``node_modules``/``customer.json`` is structurally impossible: the command
    and its pre-run steps are bound together in one place instead of being
    re-decided by every caller.
    """

    def __init__(self, worktree: "Worktree", service: str, *, overlay: "OverlayBase | None" = None) -> None:
        self.worktree = worktree
        self.service = service
        self.overlay = overlay or get_overlay_for_worktree(worktree)

    @staticmethod
    def _collect_steps(overlay: "OverlayBase", worktree: "Worktree", services: list[str]) -> "list[ProvisionStep]":
        seen: set[str] = set()
        steps: list[ProvisionStep] = []
        for service in services:
            for step in overlay.get_pre_run_steps(worktree, service):
                if step.name in seen:
                    continue
                seen.add(step.name)
                steps.append(step)
        return steps

    @classmethod
    def prepare_all(cls, worktree: "Worktree", services: list[str], *, overlay: "OverlayBase | None" = None) -> None:
        overlay = overlay or get_overlay_for_worktree(worktree)
        run_provision_steps(cls._collect_steps(overlay, worktree, services), stop_on_required_failure=False)

    def prepare(self) -> None:
        run_provision_steps(
            self._collect_steps(self.overlay, self.worktree, [self.service]),
            stop_on_required_failure=False,
        )

    def run(self) -> RunnerResult:
        self.prepare()
        cmd = self.overlay.get_run_commands(self.worktree).get(self.service)
        if not cmd:
            return RunnerResult(ok=False, detail=f"no run command configured for {self.service!r}")
        args = cmd.args if isinstance(cmd, RunCommand) else list(cmd)
        cwd = cmd.cwd if isinstance(cmd, RunCommand) else None
        rc = run_streamed(args, cwd=cwd, check=False)
        return RunnerResult(ok=rc == 0, detail=f"{self.service} finished (rc={rc})")
