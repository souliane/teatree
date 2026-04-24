import logging
import os
from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.step_runner import run_provision_steps
from teatree.core.worktree_env import write_env_cache
from teatree.timeouts import TimeoutConfig, load_timeouts
from teatree.utils.ports import find_free_ports
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)


def compose_project(worktree: Worktree) -> str:
    """Return the docker-compose project name for this worktree."""
    ticket = worktree.ticket
    return f"{worktree.repo_path}-wt{ticket.ticket_number}" if ticket else worktree.repo_path


def _compose_env(ports: dict[str, int]) -> dict[str, str]:
    frontend = ports.get("frontend", 4200)
    return {
        "BACKEND_HOST_PORT": str(ports.get("backend", 8000)),
        "FRONTEND_HOST_PORT": str(frontend),
        "POSTGRES_HOST_PORT": str(ports.get("postgres", 5432)),
        "POSTGRES_PORT": str(ports.get("postgres", 5432)),
        "CORS_WHITE_FRONT": f"http://localhost:{frontend}",
    }


def _compose_files(compose_file: str) -> list[str]:
    flags = ["-f", compose_file]
    override = Path(compose_file).parent / "docker-compose.override.yml"
    if override.is_file():
        flags.extend(["-f", str(override)])
    return flags


def docker_compose_down(project: str, *, timeout: int | None = 30) -> None:
    """Stop and remove containers for the compose project."""
    try:
        result = run_allowed_to_fail(
            ["docker", "compose", "-p", project, "down", "--remove-orphans"],
            expected_codes=None,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("docker compose down: %s", result.stderr.strip()[:300])
    except TimeoutExpired:
        logger.warning("docker compose down timed out after %ss", timeout)


class WorktreeStartRunner(RunnerBase):
    """Run the docker side-effects of ``Worktree.start_services()``.

    Executes after the FSM advances to ``SERVICES_UP``. Stops any previous
    containers, refreshes the env cache with the freshly allocated ports,
    runs overlay pre-run steps, and starts the docker-compose project.
    Idempotent: re-firing replays the down/up cycle so a partially-failed
    previous run gets a clean retry.
    """

    def __init__(
        self,
        worktree: Worktree,
        *,
        overlay: OverlayBase | None = None,
        ports: dict[str, int] | None = None,
        timeouts: TimeoutConfig | None = None,
    ) -> None:
        self.worktree = worktree
        self.overlay = overlay or get_overlay()
        self.ports = ports if ports is not None else self._allocate_ports()
        self.timeouts = timeouts or load_timeouts(self.overlay)

    def run(self) -> RunnerResult:
        worktree = self.worktree
        overlay = self.overlay
        project = compose_project(worktree)

        docker_compose_down(project, timeout=self.timeouts.get("docker_compose_down"))

        port_env = _compose_env(self.ports)
        for key, value in port_env.items():
            os.environ[key] = value

        commands = overlay.get_run_commands(worktree)
        pre_run_steps = []
        for service_name in commands:
            pre_run_steps.extend(overlay.get_pre_run_steps(worktree, service_name))
        run_provision_steps(pre_run_steps, stop_on_required_failure=False)

        write_env_cache(worktree)

        compose_file = overlay.get_compose_file(worktree)
        if not compose_file:
            return RunnerResult(ok=True, detail=f"no compose file for {worktree.repo_path}")

        env = {**os.environ, **port_env, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        if not self._docker_compose_up(project, compose_file, env):
            return RunnerResult(ok=False, detail=f"docker compose up failed for {worktree.repo_path}")

        extra = worktree.extra or {}
        extra["services"] = list(commands)
        extra["ports"] = self.ports
        worktree.extra = extra
        worktree.save(update_fields=["extra"])
        return RunnerResult(ok=True, detail=f"started {len(commands)} service(s)")

    def _docker_compose_up(self, project: str, compose_file: str, env: dict[str, str]) -> bool:
        cmd = [
            "docker",
            "compose",
            "-p",
            project,
            *_compose_files(compose_file),
            "up",
            "-d",
            "--no-build",
            "--pull=never",
        ]
        timeout = self.timeouts.get("docker_compose_up")
        try:
            result = run_allowed_to_fail(cmd, env=env, expected_codes=None, timeout=timeout)
        except TimeoutExpired:
            logger.warning("docker compose up timed out after %ss", timeout)
            return False
        if result.returncode != 0:
            logger.warning(
                "docker compose up failed (exit %s): %s",
                result.returncode,
                result.stderr.strip()[:500],
            )
            return False
        return True

    @staticmethod
    def _allocate_ports() -> dict[str, int]:
        from teatree.config import load_config  # noqa: PLC0415

        workspace = str(load_config().user.workspace_dir)
        return find_free_ports(workspace)
