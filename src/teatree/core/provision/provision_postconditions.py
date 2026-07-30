"""The aggregate provision post-condition probe (PR-27, souliane/teatree#1385).

A worktree row can claim FSM state ``provisioned`` while the artifacts that
state promises — the env cache, the application database, each provision step's
own resource — have since been deleted out from under it. The aggregate probe
here is the truth test ``worktree status`` evaluates: a ``provisioned`` worktree
is *really* provisioned only if every one of these post-conditions still holds.
Deleting ``.t3-cache/<repo>/.t3-env.cache`` or the worktree DB flips one to FAIL, so
``worktree status`` refuses green with a non-zero exit.

The probes reuse :class:`teatree.core.worktree.readiness.Probe` — a check_fn returning a
:class:`~teatree.core.worktree.readiness.ProbeResult` — so they run and report through
the same :func:`~teatree.core.worktree.readiness.run_and_report_probes` seam the runtime
readiness probes use.
"""

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from teatree.core.worktree.readiness import Probe, ProbeResult
from teatree.core.worktree.worktree_env import env_cache_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.models import Worktree
    from teatree.core.overlay import OverlayBase


class PostConditionOutcome(TypedDict):
    """One aggregate-provision post-condition outcome, JSON-serialisable for ``worktree status``."""

    name: str
    passed: bool
    reason: str


def _worktree_dir_probe(wt_path: str) -> Probe:
    def _check() -> ProbeResult:
        ok = Path(wt_path).is_dir()
        return ProbeResult(name="worktree-dir", passed=ok, reason="present" if ok else f"missing: {wt_path}")

    return Probe(name="worktree-dir", description=f"Worktree directory exists: {wt_path}", check_fn=_check)


def _env_cache_probe(cache_path: Path) -> Probe:
    def _check() -> ProbeResult:
        ok = cache_path.is_file()
        return ProbeResult(name="env-cache", passed=ok, reason="present" if ok else f"missing: {cache_path}")

    return Probe(name="env-cache", description=f"Env cache written: {cache_path}", check_fn=_check)


def _app_db_probe(db_name: str) -> Probe:
    def _check() -> ProbeResult:
        from teatree.utils.db import db_exists  # noqa: PLC0415 — psql shell-out kept off import path

        ok = db_exists(db_name)
        return ProbeResult(name="app-db", passed=ok, reason="exists" if ok else f"database {db_name!r} does not exist")

    return Probe(name="app-db", description=f"Application database exists: {db_name}", check_fn=_check)


def _step_post_condition_probe(step_name: str, condition: "Callable[[], bool]") -> Probe:
    name = f"step:{step_name}"

    def _check() -> ProbeResult:
        try:
            held = bool(condition())
        except Exception as exc:  # noqa: BLE001 — a raising post-condition is a FAIL, not a crash
            return ProbeResult(name=name, passed=False, reason=f"{type(exc).__name__}: {exc}")
        return ProbeResult(name=name, passed=held, reason="held" if held else "post-condition not satisfied")

    return Probe(name=name, description=f"Provision step post-condition holds: {step_name}", check_fn=_check)


def aggregate_provision_post_conditions(overlay: "OverlayBase", worktree: "Worktree") -> list[Probe]:
    """Return every post-condition a ``provisioned`` *worktree* must still satisfy (PR-27).

    The set is the union of the core provisioning invariants (the worktree
    directory is on disk, the env cache is written, and — when the overlay
    imports a database — that database exists) and each provision step's own
    ``post_condition``. An empty list (a worktree with no on-disk path yet)
    means "nothing to verify", which a caller reads as trivially satisfied.
    """
    probes: list[Probe] = []
    wt_path = worktree.worktree_path
    if wt_path:
        probes.append(_worktree_dir_probe(wt_path))
        cache = env_cache_path(worktree)
        if cache is not None:
            probes.append(_env_cache_probe(cache))
    if worktree.db_name and overlay.provisioning.db_import_strategy(worktree) is not None:
        probes.append(_app_db_probe(worktree.db_name))
    probes.extend(
        _step_post_condition_probe(step.name, condition)
        for step in overlay.get_provision_steps(worktree)
        if (condition := step.post_condition) is not None
    )
    return probes


def evaluate_post_conditions(overlay: "OverlayBase", worktree: "Worktree") -> tuple[list[PostConditionOutcome], int]:
    """Run every aggregate post-condition for *worktree*; return ``(outcomes, failure_count)``.

    The seam ``worktree status`` uses to decide the exit code: a non-zero
    failure count means the ``provisioned`` worktree is not truly provisioned.
    """
    outcomes: list[PostConditionOutcome] = []
    failures = 0
    for probe in aggregate_provision_post_conditions(overlay, worktree):
        result = probe.check()
        outcomes.append({"name": result.name, "passed": result.passed, "reason": result.reason})
        if not result.passed:
            failures += 1
    return outcomes, failures
