import json
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

# Build can take minutes on first start — much longer than the 60s default for `up`.
DOCKER_COMPOSE_BUILD_TIMEOUT = 600


def compose_project(worktree: Worktree) -> str:
    """Return the docker-compose project name for this worktree."""
    ticket = worktree.ticket
    return f"{worktree.repo_path}-wt{ticket.ticket_number}" if ticket else worktree.repo_path


def _compose_env(ports: dict[str, int], overlay: OverlayBase) -> dict[str, str]:
    """Render ``${KEY}_HOST_PORT`` env vars (plus overlay-specific aliases) for compose."""
    return overlay.get_port_env(ports)


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
        self.ports = ports if ports is not None else self._allocate_ports(self.overlay, worktree)
        self.timeouts = timeouts or load_timeouts(self.overlay)

    def run(self) -> RunnerResult:
        worktree = self.worktree
        overlay = self.overlay
        project = compose_project(worktree)

        docker_compose_down(project, timeout=self.timeouts.get("docker_compose_down"))

        port_env = _compose_env(self.ports, overlay)
        for key, value in port_env.items():
            os.environ[key] = value

        commands = overlay.get_run_commands(worktree)
        # Dedupe by step name — overlays commonly return the same provisioning
        # steps for related services (e.g. frontend + build-frontend share setup).
        seen_step_names: set[str] = set()
        pre_run_steps = []
        for service_name in commands:
            for step in overlay.get_pre_run_steps(worktree, service_name):
                if step.name in seen_step_names:
                    continue
                seen_step_names.add(step.name)
                pre_run_steps.append(step)
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
        self._ensure_images_built(project, compose_file, env)
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

    def _ensure_images_built(self, project: str, compose_file: str, env: dict[str, str]) -> None:
        """Build any compose service whose ``build:`` image is missing locally.

        ``up --no-build --pull=never`` fails the first time a worktree's
        compose override references a service that builds from a Dockerfile
        (e.g. an overlay's client-term-redacted sidecar) — neither pull nor build runs, so
        the missing image is a hard error. Pre-flight per service, build the
        missing ones once, then let the subsequent ``up`` reuse the local
        images on every later start.
        """
        services = self._enumerate_buildable_services(project, compose_file, env)
        if not services:
            return
        missing = sorted(name for name, image in services.items() if not self._image_exists(image, env))
        if not missing:
            return
        logger.info("Building missing compose images for services: %s", missing)
        cmd = [
            "docker",
            "compose",
            "-p",
            project,
            *_compose_files(compose_file),
            "build",
            *missing,
        ]
        timeout = self.timeouts.get("docker_compose_build") or DOCKER_COMPOSE_BUILD_TIMEOUT
        try:
            result = run_allowed_to_fail(cmd, env=env, expected_codes=None, timeout=timeout)
        except TimeoutExpired:
            logger.warning("docker compose build timed out after %ss", timeout)
            return
        if result.returncode != 0:
            logger.warning(
                "docker compose build failed (exit %s): %s",
                result.returncode,
                result.stderr.strip()[:500],
            )

    @staticmethod
    def _enumerate_buildable_services(project: str, compose_file: str, env: dict[str, str]) -> dict[str, str]:
        """Return ``{service_name: resolved_image_tag}`` for services with a ``build:`` section.

        Returns an empty dict on any failure — older compose versions without
        ``--format json``, unreachable daemons, malformed configs. The caller
        treats an empty dict as "nothing to preflight" and lets ``up`` proceed
        naturally; if ``up`` then fails on a missing image, the existing
        warning path surfaces the cause.
        """
        cmd = [
            "docker",
            "compose",
            "-p",
            project,
            *_compose_files(compose_file),
            "config",
            "--format",
            "json",
        ]
        try:
            result = run_allowed_to_fail(cmd, env=env, expected_codes=None, timeout=30)
        except TimeoutExpired:
            logger.warning("docker compose config timed out — skipping image preflight")
            return {}
        if result.returncode != 0:
            logger.info(
                "docker compose config failed (exit %s) — skipping image preflight",
                result.returncode,
            )
            return {}
        try:
            config = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.info("docker compose config returned non-JSON — skipping image preflight")
            return {}
        if not isinstance(config, dict):
            return {}
        services: dict[str, str] = {}
        for name, spec in (config.get("services") or {}).items():
            if isinstance(spec, dict) and "build" in spec and isinstance(spec.get("image"), str):
                services[name] = spec["image"]
        return services

    @staticmethod
    def _image_exists(image: str, env: dict[str, str]) -> bool:
        cmd = ["docker", "image", "inspect", image]
        try:
            result = run_allowed_to_fail(cmd, env=env, expected_codes=None, timeout=10)
        except TimeoutExpired:
            return False
        return result.returncode == 0

    @staticmethod
    def _allocate_ports(overlay: OverlayBase, worktree: Worktree) -> dict[str, int]:
        from teatree.config import load_config  # noqa: PLC0415

        workspace = str(load_config().user.workspace_dir)
        return find_free_ports(workspace, overlay.get_required_ports(worktree))
