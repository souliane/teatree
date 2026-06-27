import logging
import os
from pathlib import Path

from teatree.core import prek_hook
from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay_for_worktree
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.step_runner import ProvisionReport, run_provision_steps, run_step
from teatree.core.worktree_env import CACHE_FILENAME, worktree_pg_connection, write_env_cache

logger = logging.getLogger(__name__)


def heal_missing_provisioned_db(worktree: Worktree, overlay: OverlayBase) -> bool:
    """Re-provision the DB when a ``provisioned`` worktree's DB is gone (#1038).

    An interrupted provision (killed between the FSM flip to PROVISIONED and the
    DB import) leaves a worktree whose ``db_name`` is set, whose overlay declares
    a DB import strategy, but whose Postgres DB was never created — so a later
    ``start`` fails its runtime probe with "database does not exist". This detects
    that exact gap and re-runs the idempotent provision runner to recreate the DB.

    Returns ``True`` when a re-provision ran and succeeded, ``False`` when nothing
    needed healing (no DB strategy, no ``db_name``, DB already present, or an
    unresolvable connection probe — fail-safe: an ambiguous probe never triggers
    a needless re-import). Raises ``RuntimeError`` when the re-provision itself
    fails, so the caller can surface a hard failure rather than start a broken
    stack.
    """
    from teatree.utils.db import db_exists  # noqa: PLC0415

    if not worktree.db_name or overlay.get_db_import_strategy(worktree) is None:
        return False
    try:
        user, host, env = worktree_pg_connection(worktree, overlay=overlay)
        if db_exists(worktree.db_name, user=user, host=host, env=env or None):
            return False
    except Exception:  # noqa: BLE001 — fail-safe: an unresolvable probe must never trigger a re-import
        logger.debug("DB existence probe inconclusive for %s — skipping heal", worktree.db_name)
        return False
    logger.info("DB %s missing for provisioned worktree — re-provisioning before start", worktree.db_name)
    result = WorktreeProvisionRunner(worktree, overlay=overlay).run()
    worktree.refresh_from_db()
    if not result.ok:
        msg = f"DB re-provision failed for {worktree.repo_path}: {result.detail}"
        raise RuntimeError(msg)
    return True


def _append_envrc_lines(wt_path: str, lines: list[str]) -> None:
    """Idempotently add missing direnv lines to the worktree's ``.envrc``."""
    envrc = Path(wt_path) / ".envrc"
    existing = envrc.read_text() if envrc.is_file() else ""
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        envrc.write_text(existing.rstrip() + "\n" + "\n".join(missing) + "\n")


def _setup_worktree_dir(wt_path: str, worktree: Worktree, overlay: OverlayBase) -> str | None:
    """Configure direnv + prek for the worktree directory.

    Returns ``None`` on success or a short failure detail when ``prek
    install`` cannot install the hook scripts. A non-``None`` return is the
    caller's signal to refuse to mark the worktree provisioned — without an
    installed pre-commit hook the worktree is a silent-bypass surface for
    every gate the project enforces at commit time (souliane/teatree#1253).

    ``direnv allow`` failures are kept warning-only — direnv is a developer
    convenience, not a correctness gate. ``prek install`` is upgraded to an
    error because a missing pre-commit hook is a hard correctness regression
    (migration-scoping, banned-terms, privacy guard all rely on it firing).
    """
    if not wt_path or not Path(wt_path).is_dir():
        return None
    core_lines = [f"dotenv {CACHE_FILENAME}"]
    _append_envrc_lines(wt_path, core_lines + overlay.get_envrc_lines(worktree))
    result = run_step("direnv-allow", ["direnv", "allow", wt_path], check=False)
    if not result.success:
        logger.warning("direnv allow failed: %s", result.error)
    if (Path(wt_path) / ".pre-commit-config.yaml").is_file():
        result = prek_hook.install(wt_path)
        if not result.success:
            logger.error("prek install failed: %s", result.error)
            return f"prek install failed: {result.error}"
    return None


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
        self.overlay = overlay or get_overlay_for_worktree(worktree)
        self.slow_import = slow_import

    def run(self) -> RunnerResult:
        worktree = self.worktree
        overlay = self.overlay

        spec = write_env_cache(worktree, overlay=overlay)
        if spec:
            logger.info("Wrote env cache: %s", spec.path)

        wt_path = (worktree.extra or {}).get("worktree_path", "")
        setup_failure = _setup_worktree_dir(wt_path, worktree, overlay)
        if setup_failure is not None:
            return RunnerResult(ok=False, detail=setup_failure)

        db_import_needed = worktree.db_name and overlay.get_db_import_strategy(worktree) is not None
        if db_import_needed and not self._run_db_import():
            return RunnerResult(ok=False, detail="db import failed")

        report = run_provision_steps(overlay.get_provision_steps(worktree))
        post_db_report = self._run_post_db_steps()
        pre_run_report = self._run_pre_run_steps()
        report.steps.extend(post_db_report.steps + pre_run_report.steps)

        health_failures = self._run_health_checks()

        if not report.success:
            failed = report.failed_required_step or "unknown"
            return RunnerResult(ok=False, detail=f"step '{failed}' failed")
        if health_failures:
            return RunnerResult(ok=False, detail=f"health checks failed: {', '.join(health_failures)}")
        return RunnerResult(ok=True, detail=f"{len(report.steps)} step(s) ok")

    def _run_db_import(self) -> bool:
        from teatree.utils.db import db_exists  # noqa: PLC0415

        worktree = self.worktree
        overlay = self.overlay

        # Fail loud before importing into a db_name another ticket's live
        # worktree already owns — never clobber a foreign database (#WT-PR-D).
        worktree.assert_db_name_unclaimed()

        if worktree.db_name:
            user, host, env = worktree_pg_connection(worktree, overlay=overlay)
            try:
                if db_exists(worktree.db_name, user=user, host=host, env=env or None):
                    logger.info("DB exists: %s — skipping import", worktree.db_name)
                    return True
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
            return True
        logger.error("DB import failed for %s — aborting provision", worktree.repo_path)
        return False

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
