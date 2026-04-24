import logging

from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.base import RunnerBase, RunnerResult

logger = logging.getLogger(__name__)


class WorktreeVerifyRunner(RunnerBase):
    """Run overlay health checks and record service URLs on the worktree.

    Executes after ``Worktree.verify()`` flips SERVICES_UP → READY. Each
    overlay-declared check runs once; failures are listed in the result
    detail without raising so the worker treats verify as best-effort.
    """

    def __init__(self, worktree: Worktree, *, overlay: OverlayBase | None = None) -> None:
        self.worktree = worktree
        self.overlay = overlay or get_overlay()

    def run(self) -> RunnerResult:
        checks = self.overlay.get_health_checks(self.worktree)
        failures: list[str] = []
        for check in checks:
            try:
                if not check.check():
                    failures.append(check.name)
                    logger.warning("Health check failed: %s — %s", check.name, check.description)
            except Exception:
                failures.append(check.name)
                logger.exception("Health check error: %s", check.name)

        if failures:
            return RunnerResult(ok=False, detail=f"failed: {', '.join(failures)}")
        return RunnerResult(ok=True, detail=f"{len(checks)} check(s) ok")
