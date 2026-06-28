import json
import logging
import os
from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay_for_worktree
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.runners.service_launch import ServiceLauncher
from teatree.core.worktree_env import compose_project, write_env_cache
from teatree.timeouts import DOCKER_COMPOSE_BUILD, DOCKER_COMPOSE_DOWN, DOCKER_COMPOSE_UP, TimeoutConfig, load_timeouts
from teatree.utils.ports import get_worktree_ports
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)


def _compose_files(compose_file: str) -> list[str]:
    flags = ["-f", compose_file]
    override = Path(compose_file).parent / "docker-compose.override.yml"
    if override.is_file():
        flags.extend(["-f", str(override)])
    return flags


def docker_compose_down(project: str, *, timeout: int | None = 30, remove_volumes: bool = False) -> None:
    """Stop and remove containers for the compose project.

    ``remove_volumes`` adds ``--volumes`` so the project's named/anonymous volumes
    are torn down too — the done-worktree wipe passes it (a reaped worktree owns
    its docker volumes, and leaving them behind is a slow disk leak). The
    start-time reset leaves it off, so a restart never wipes a volume holding the
    worktree's state.

    Tolerant of an unavailable docker binary (CI sandboxes, hermetic test
    environments): a ``FileNotFoundError`` / ``PermissionError`` from
    ``subprocess.run`` is logged and swallowed so cleanup paths that funnel
    through here (#1306) don't break when there's no docker to talk to.
    """
    cmd = ["docker", "compose", "-p", project, "down", "--remove-orphans"]
    if remove_volumes:
        cmd.append("--volumes")
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, timeout=timeout)
        if result.returncode != 0:
            logger.warning("docker compose down: %s", result.stderr.strip()[:300])
    except TimeoutExpired:
        logger.warning("docker compose down timed out after %ss", timeout)
    except (FileNotFoundError, PermissionError) as exc:
        logger.debug("docker compose down skipped — docker unavailable: %s", exc)


class WorktreeStartRunner(RunnerBase):
    """Run the docker side-effects of ``Worktree.start_services()``.

    Executes after the FSM advances to ``SERVICES_UP``. Stops any previous
    containers, runs overlay pre-run steps, brings up docker-compose, then
    queries the auto-mapped host ports and stores them in
    ``Worktree.extra["ports"]``. Idempotent: re-firing replays the down/up
    cycle so a partially-failed previous run gets a clean retry.
    """

    def __init__(
        self,
        worktree: Worktree,
        *,
        overlay: OverlayBase | None = None,
        timeouts: TimeoutConfig | None = None,
    ) -> None:
        self.worktree = worktree
        self.overlay = overlay or get_overlay_for_worktree(worktree)
        self.timeouts = timeouts or load_timeouts(self.overlay)

    def run(self) -> RunnerResult:
        worktree = self.worktree
        overlay = self.overlay
        project = compose_project(worktree)

        docker_compose_down(project, timeout=self.timeouts.get(DOCKER_COMPOSE_DOWN))

        commands = overlay.get_run_commands(worktree)
        ServiceLauncher.prepare_all(worktree, list(commands), overlay=overlay)

        write_env_cache(worktree, overlay=overlay)

        compose_file = overlay.get_compose_file(worktree)
        if not compose_file:
            return RunnerResult(ok=True, detail=f"no compose file for {worktree.repo_path}")

        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        ok, reason = self._docker_compose_up(project, compose_file, env)
        if not ok:
            return RunnerResult(
                ok=False,
                detail=f"docker compose up failed for {worktree.repo_path}: {reason}",
            )

        ports = get_worktree_ports(project, compose_file=compose_file)
        extra = worktree.extra or {}
        extra["services"] = list(commands)
        extra["ports"] = ports
        worktree.extra = extra
        worktree.save(update_fields=["extra"])
        return RunnerResult(ok=True, detail=f"started {len(commands)} service(s)")

    def _docker_compose_up(self, project: str, compose_file: str, env: dict[str, str]) -> tuple[bool, str]:
        """Bring the stack up.

        Returns ``(ok, reason)`` — *reason* carries the real failure cause
        (build error, timeout, or compose stderr) so the caller can surface
        it instead of a generic "failed" message.
        """
        build_error = self._ensure_images_built(project, compose_file, env)
        if build_error:
            return False, build_error
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
        timeout = self.timeouts.get(DOCKER_COMPOSE_UP)
        try:
            result = run_allowed_to_fail(cmd, env=env, expected_codes=None, timeout=timeout)
        except TimeoutExpired:
            msg = f"compose up timed out after {timeout}s"
            logger.warning(msg)
            return False, msg
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning("docker compose up failed (exit %s): %s", result.returncode, stderr[:500])
            tail = stderr.splitlines()[-1] if stderr else "(no stderr)"
            return False, f"exit {result.returncode}: {tail}"
        return True, ""

    def _ensure_images_built(self, project: str, compose_file: str, env: dict[str, str]) -> str | None:
        """Build any compose service whose ``build:`` image is missing locally.

        ``up --no-build --pull=never`` fails the first time a worktree's
        compose override references a service that builds from a Dockerfile
        (e.g. an overlay's locally-built sidecar) — neither pull nor build runs, so
        the missing image is a hard error. Pre-flight per service, build the
        missing ones once, then let the subsequent ``up`` reuse the local
        images on every later start.

        Returns ``None`` on success, or a short error string when the build
        fails or times out so the caller surfaces the real cause instead of a
        generic "docker compose up failed".
        """
        services = self._enumerate_buildable_services(project, compose_file, env)
        if not services:
            return None
        missing = sorted(name for name, image in services.items() if not self._image_exists(image, env))
        if not missing:
            return None
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
        timeout = self.timeouts.get(DOCKER_COMPOSE_BUILD)
        try:
            result = run_allowed_to_fail(cmd, env=env, expected_codes=None, timeout=timeout)
        except TimeoutExpired:
            msg = f"image build for {missing} timed out after {timeout}s"
            logger.warning(msg)
            return msg
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning("docker compose build failed (exit %s): %s", result.returncode, stderr[:500])
            tail = stderr.splitlines()[-1] if stderr else "(no stderr)"
            return f"image build for {missing} failed (exit {result.returncode}): {tail}"
        return None

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
