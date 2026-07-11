import logging
import time
from pathlib import Path

from teatree.config import get_effective_settings
from teatree.core import prek_hook
from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay_for_worktree
from teatree.core.provision.provision_timebox import alert_provision_user, run_timeboxed_db_import
from teatree.core.provision.step_runner import ProvisionReport, StepResult, run_provision_steps, run_step
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME, worktree_pg_connection, write_env_cache
from teatree.utils.env import patched_environ

logger = logging.getLogger(__name__)

# Matches ``UserSettings.provision_slow_threshold_seconds``'s dataclass
# default — the fallback when the setting is absent/unreadable, mirroring
# ``provision_timebox.resolve_step_timeout_seconds``'s defensive coercion.
_DEFAULT_SLOW_THRESHOLD_SECONDS = 600


def _resolve_slow_threshold_seconds() -> int:
    """The configured slow-provision alert threshold (seconds), defensively coerced.

    A non-numeric or unreadable setting (e.g. an under-specified settings
    mock in a caller's test) degrades to the default rather than crashing the
    provision — the alert is a best-effort nicety, never a correctness gate.
    """
    value = getattr(get_effective_settings(), "provision_slow_threshold_seconds", _DEFAULT_SLOW_THRESHOLD_SECONDS)
    try:
        return int(value)
    except (TypeError, ValueError):
        return _DEFAULT_SLOW_THRESHOLD_SECONDS


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

    if not worktree.db_name or overlay.provisioning.db_import_strategy(worktree) is None:
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
    repo_name = Path(wt_path).name
    core_lines = [f"dotenv ../{CACHE_DIRNAME}/{repo_name}/{CACHE_FILENAME}"]
    _append_envrc_lines(wt_path, core_lines + overlay.provisioning.envrc_lines(worktree))
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

        # #2919: an auto-isolated per-worktree self-DB (teatree.paths) is seeded
        # once from a canonical snapshot and never re-migrated on its own — a
        # provision step below reads settings (e.g. provision_timebox's
        # get_effective_settings()), and a stored ConfigSetting row that predates
        # a since-added migration crashes that read with a raw ValueError. Self-heal
        # the self-DB schema first, exactly like the sanctioned merge path (#2006).
        try:
            require_current_schema()
        except SelfDbMigrationError as exc:
            return RunnerResult(ok=False, detail=str(exc))

        spec = write_env_cache(worktree, overlay=overlay)
        if spec:
            logger.info("Wrote env cache: %s", spec.path)

        wt_path = (worktree.extra or {}).get("worktree_path", "")
        setup_failure = _setup_worktree_dir(wt_path, worktree, overlay)
        if setup_failure is not None:
            return RunnerResult(ok=False, detail=setup_failure)

        report = ProvisionReport()
        db_import_needed = worktree.db_name and overlay.provisioning.db_import_strategy(worktree) is not None
        if db_import_needed:
            db_step = self._run_db_import_timed()
            report.steps.append(db_step)
            if not db_step.success:
                self._persist_report(report)
                return RunnerResult(ok=False, detail="db import failed")

        step_report = run_provision_steps(overlay.get_provision_steps(worktree))
        post_db_report = self._run_post_db_steps()
        pre_run_report = self._run_pre_run_steps()
        report.steps.extend(step_report.steps + post_db_report.steps + pre_run_report.steps)

        health_failures = self._run_health_checks()

        self._persist_report(report)

        if not report.success:
            failed = report.failed_required_step or "unknown"
            return RunnerResult(ok=False, detail=f"step '{failed}' failed")
        if health_failures:
            return RunnerResult(ok=False, detail=f"health checks failed: {', '.join(health_failures)}")
        return RunnerResult(ok=True, detail=f"{len(report.steps)} step(s) ok")

    def _persist_report(self, report: ProvisionReport) -> None:
        """Persist *report* to ``Worktree.extra['provision_report']`` and log/alert (souliane/teatree#2949).

        No schema change — ``Worktree.extra`` is existing JSON. A one-line
        summary always logs; a total exceeding ``provision_slow_threshold_seconds``
        additionally fires the best-effort out-of-band user alert so a
        provisioning-speed regression is never silently absorbed.
        """
        extra = self.worktree.extra or {}
        extra["provision_report"] = report.to_dict()
        self.worktree.extra = extra
        self.worktree.save(update_fields=["extra"])
        logger.info(
            "provision(%s): %d step(s), %.1fs total, %s",
            self.worktree.repo_path,
            len(report.steps),
            report.total_duration,
            "OK" if report.success else f"FAILED at {report.failed_required_step}",
        )
        threshold = _resolve_slow_threshold_seconds()
        if report.total_duration > threshold:
            alert_provision_user(
                step="provision",
                repo=self.worktree.repo_path,
                detail=f"total duration {report.total_duration:.0f}s exceeded the {threshold}s threshold",
            )

    def _run_db_import_timed(self) -> StepResult:
        start = time.monotonic()
        ok = self._run_db_import()
        duration = time.monotonic() - start
        return StepResult(name="db_import", success=ok, duration=duration, required=True)

    def _run_db_import(self) -> bool:
        from teatree.utils.db import db_exists  # noqa: PLC0415
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415 — paired with the local db_exists import above

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
            except (FileNotFoundError, CommandFailedError):
                # Existence is unknown (psql binary missing, or the server was
                # unreachable) — fall through to the import attempt, which fails
                # loud on its own if the server is genuinely down (#3094).
                pass

        # The overlay env (and the VIRTUAL_ENV drop that keeps host pg tools off the
        # loop's venv) is scoped to the import so it never bleeds into the next
        # provision of the long-lived loop process.
        # #2244: a child blocked on its PIPE (no DSLR snapshot) must abort loud, never hang the provision.
        with patched_environ(overlay.provisioning.env_extra(worktree), remove=("VIRTUAL_ENV",)):
            imported = run_timeboxed_db_import(
                lambda: overlay.provisioning.db_import(worktree, slow_import=self.slow_import),
                repo=worktree.repo_path,
            )
        if imported:
            extra = worktree.extra or {}
            extra.pop("db_import_failures", None)
            worktree.extra = extra
            worktree.save(update_fields=["extra"])
            return True
        logger.error("DB import failed for %s — aborting provision", worktree.repo_path)
        return False

    def _run_post_db_steps(self) -> ProvisionReport:
        steps = list(self.overlay.provisioning.post_db_steps(self.worktree))
        reset_step = self.overlay.provisioning.reset_passwords_command(self.worktree)
        if reset_step:
            steps.append(reset_step)
        return run_provision_steps(steps, stop_on_required_failure=False)

    def _run_pre_run_steps(self) -> ProvisionReport:
        steps = []
        for service_name in self.overlay.runtime.run_commands(self.worktree):
            steps.extend(self.overlay.runtime.pre_run_steps(self.worktree, service_name))
        return run_provision_steps(steps, stop_on_required_failure=False)

    def _run_health_checks(self) -> list[str]:
        checks = self.overlay.provisioning.health_checks(self.worktree)
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
