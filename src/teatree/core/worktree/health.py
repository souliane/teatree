"""Post-provision health checks for worktree readiness."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models import Worktree
    from teatree.core.overlay import OverlayProvisioning


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    check: Callable[[], bool]
    description: str = ""


def _symlink_source_healthy(dest: Path, source: Path) -> bool:
    if dest.is_symlink():
        if not source.exists():
            return False
        if source.is_dir():
            return any(source.iterdir())
        return True
    if not dest.exists():
        return False
    if dest.is_dir():
        return any(dest.iterdir())
    return True


def default_health_checks(provisioning: "OverlayProvisioning", worktree: "Worktree") -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    extra = worktree.extra or {}
    wt_path = extra.get("worktree_path", "")

    if wt_path:
        checks.append(
            HealthCheck(
                name="worktree-exists",
                check=lambda: Path(wt_path).is_dir(),
                description=f"Worktree directory exists: {wt_path}",
            ),
        )

        for spec in provisioning.symlinks(worktree):
            dest = Path(wt_path) / spec.get("path", "")
            source = Path(spec.get("source", ""))
            if spec.get("mode", "symlink") == "symlink" and source.exists():
                checks.append(
                    HealthCheck(
                        name=f"symlink-{spec.get('path', '?')}",
                        check=lambda d=dest, s=source: _symlink_source_healthy(d, s),
                        description=f"Symlink target populated: {spec.get('path', '')}",
                    ),
                )

    return checks
