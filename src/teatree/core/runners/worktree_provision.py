import logging
import os
from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.step_runner import ProvisionReport, run_provision_steps, run_step
from teatree.core.worktree_env import CACHE_FILENAME, write_env_cache

logger = logging.getLogger(__name__)


def _append_envrc_lines(wt_path: str, lines: list[str]) -> None:
    """Idempotently add missing direnv lines to the worktree's ``.envrc``."""
    envrc = Path(wt_path) / ".envrc"
    existing = envrc.read_text() if envrc.is_file() else ""
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        envrc.write_text(existing.rstrip() + "\n" + "\n".join(missing) + "\n")


def _setup_worktree_dir(wt_path: str, worktree: Worktree, overlay: OverlayBase) -> None:
    """Configure direnv + prek for the worktree directory."""
    if not wt_path or not Path(wt_path).is_dir():
        return
    core_lines = [f"dotenv {CACHE_FILENAME}"]
    _append_envrc_lines(wt_path, core_lines + overlay.get_envrc_lines(worktree))
    result = run_step("direnv-allow", ["direnv", "allow", wt_path], check=False)
    if not result.success:
        logger.warning("direnv allow failed: %s", result.error)
    if (Path(wt_path) / ".pre-commit-config.yaml").is_file():
        result = run_step("prek-install", ["prek", "install", "-f"], cwd=wt_path, check=False)
        if not result.success:
            logger.warning("prek install failed: %s", result.error)


class WorktreeProvisionRunner(RunnerBase):
    """Run the heavy provisioning side-effects of ``Worktree.provision()``.

    Executes after the FSM has flipped CREATED → PROVISIONED. Writes the env
    cache, configures direnv + prek, runs the overlay's DB import and
    provision/post-db/pre-run steps, and finally invokes the overlay's
    health checks. Idempotent: ``write_env_cache`` rewrites cleanly, every
    overlay step is expected to be re-runnable, and ``db_import`` no-ops
    when the DB already exists.
    """

    def __init__(
        self,
        worktree: Worktree,
        *,
        overlay: OverlayBase | None = None,
        slow_import: bool = False,
    ) -> None:
        self.worktree = worktree
        self.overlay = overlay or get_overlay()
        self.slow_import = slow_import

    def run(self) -> RunnerResult:
        worktree = self.worktree
        overlay = self.overlay

        spec = write_env_cache(worktree)
        if spec:
            logger.info("Wrote env cache: %s", spec.path)

        wt_path = (worktree.extra or {}).get("worktree_path", "")
        _setup_worktree_dir(wt_path, worktree, overlay)

        if worktree.db_name and overlay.get_db_import_strategy(worktree) is not None:
            self._run_db_import()

        report = run_provision_steps(overlay.get_provision_steps(worktree))
        post_db_report = self._run_post_db_steps()
        pre_run_report = self._run_pre_run_steps()
        report.steps.extend(post_db_report.steps + pre_run_report.steps)

        health_failures = self._run_health_checks()

        if not report.success:
            failed = report.failed_step or "unknown"
            return RunnerResult(ok=False, detail=f"step '{failed}' failed")
        if health_failures:
            return RunnerResult(ok=False, detail=f"health checks failed: {', '.join(health_failures)}")
        return RunnerResult(ok=True, detail=f"{len(report.steps)} step(s) ok")

    def _run_db_import(self) -> None:
        from teatree.utils.db import db_exists  # noqa: PLC0415

        worktree = self.worktree
        overlay = self.overlay

        if worktree.db_name:
            try:
                if db_exists(worktree.db_name):
                    logger.info("DB exists: %s — skipping import", worktree.db_name)
                    return
            except FileNotFoundError:
                pass

        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        os.environ.update(env)
        if overlay.db_import(worktree, slow_import=self.slow_import):
            extra = worktree.extra or {}
            extra.pop("db_import_failures", None)
            worktree.extra = extra
            worktree.save(update_fields=["extra"])
        else:
            logger.warning("DB import failed for %s — continuing", worktree.repo_path)

    def _run_post_db_steps(self) -> ProvisionReport:
        steps = list(self.overlay.get_post_db_steps(self.worktree))
        reset_step = self.overlay.get_reset_passwords_command(self.worktree)
        if reset_step:
            steps.append(reset_step)
        return run_provision_steps(steps, stop_on_required_failure=False)

    def _run_pre_run_steps(self) -> ProvisionReport:
        steps = []
        for service_name in self.overlay.get_run_commands(self.worktree):
            steps.extend(self.overlay.get_pre_run_steps(self.worktree, service_name))
        return run_provision_steps(steps, stop_on_required_failure=False)

    def _run_health_checks(self) -> list[str]:
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
        return failures
