"""The push-gate driver: plan the two scoped sweeps, then run them (#122).

The push-stage hook must never run the whole local suite (``#112/#21/#38``). This
module turns a diff into a :class:`PushGatePlan` — either whole-tree FULL (the
default branch: flag OFF, or any FULL trigger) or SCOPED to the changed files —
and :func:`run_push_gate` executes the two engines behind it:

*   Engine A — the ``--doctest-modules`` sweep, scoped to the changed
    ``src/teatree/**/*.py`` (doctest failures are LOCAL to the changed module, so
    no import graph is needed; the non-local cases are FULL triggers).
*   Engine B — the ast-grep regression scan, scoped to the changed src + test files
    (:func:`teatree.quality.regression_scan.scan_findings` with ``paths=``).

Safety doctrine (mirrors :mod:`teatree.quality.changed_set`): over-run is free,
under-run is a false green. Every uncertainty ⇒ FULL. A missing ast-grep engine is
DEFERRED to the CI backstop with a LOUD notice (R7) — never silently green, never a
wedged push; CI's whole-tree scan is the guarantor. The whole-tree CI backstop is
never on the push path alone.
"""

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.changed_set import ChangedSet, ChangedSetError, changed_paths, classify
from teatree.quality.regression_scan import AstGrepUnavailableError, scan_findings
from teatree.utils.run import run_allowed_to_fail

# The whole-tree doctest target — the FULL branch's ``--doctest-modules`` argument,
# byte-identical to the pre-#122 hook's ``--doctest-modules src/teatree``.
WHOLE_TREE_DOCTEST = Path("src/teatree")

_FLAG_OFF_REASON = "incremental_push_gate is OFF — whole-tree doctest + whole-tree ast-grep (default-safe, == today)"

# pytest's EXIT_NOTESTSCOLLECTED. A doctest target with no ``>>>`` example collects
# nothing and pytest exits 5 — teatree is near-zero-comments, so most modules have
# no doctests. That is NOT a doctest failure (only exit 1 is); the gate must pass.
_PYTEST_NO_TESTS_COLLECTED = 5


@dataclass(frozen=True)
class PushGatePlan:
    """A resolved push-gate decision: whole-tree FULL, or the scoped file lists.

    ``doctest_targets`` are the ``--doctest-modules`` arguments — ``(src/teatree,)``
    when FULL, else the changed src modules. ``astgrep_scope`` is ``None`` for the
    whole-tree scan (the CI-identical path) or the tuple of scoped src+test files.
    """

    is_full: bool
    reason: str
    doctest_targets: tuple[Path, ...]
    astgrep_scope: tuple[Path, ...] | None
    enabled: bool

    def report(self) -> str:
        if self.is_full:
            return f"push-gate: FULL — {self.reason}"
        return (
            f"push-gate: SCOPED — {len(self.doctest_targets)} doctest module(s), "
            f"{len(self.astgrep_scope or ())} ast-grep file(s) of the diff; full-run triggers: none"
        )


@dataclass(frozen=True)
class PushGateResult:
    ok: bool
    doctest_ok: bool
    astgrep_findings: tuple[dict, ...]
    astgrep_deferred: bool
    notes: tuple[str, ...]


def _full_plan(reason: str, *, enabled: bool) -> PushGatePlan:
    return PushGatePlan(
        is_full=True,
        reason=reason,
        doctest_targets=(WHOLE_TREE_DOCTEST,),
        astgrep_scope=None,
        enabled=enabled,
    )


def plan_push_gate(changed: ChangedSet, *, enabled: bool) -> PushGatePlan:
    """Decide the push-gate plan for *changed* under the ``incremental_push_gate`` flag.

    ``enabled=False`` ⇒ whole-tree FULL regardless of the diff (zero push-behaviour
    change on merge). ``enabled=True`` ⇒ scoped when the diff is provably local,
    FULL on any :func:`teatree.quality.changed_set.classify` trigger (the default
    branch). Pure over its arguments — the flag and diff are the only inputs.
    """
    if not enabled:
        return _full_plan(_FLAG_OFF_REASON, enabled=False)
    trigger = classify(changed)
    if trigger.full:
        return _full_plan(trigger.reason, enabled=True)
    scope = tuple(sorted(set(trigger.scoped_src) | set(trigger.scoped_tests)))
    return PushGatePlan(
        is_full=False,
        reason=trigger.reason,
        doctest_targets=trigger.scoped_src,
        astgrep_scope=scope,
        enabled=True,
    )


def resolve_plan(base_ref: str, *, enabled: bool, cwd: Path | None = None) -> PushGatePlan:
    """Gather the changed set and plan it, forcing FULL when the diff can't be computed.

    A dirty/shallow merge-base (``ChangedSetError``) is R7: a gate that cannot
    compute its selection runs the whole tree, never skips-as-pass.
    """
    try:
        changed = changed_paths(base_ref=base_ref, cwd=cwd)
    except ChangedSetError as exc:
        return _full_plan(f"could not compute the changed set ({exc}) — FULL (fail-safe)", enabled=enabled)
    return plan_push_gate(changed, enabled=enabled)


def _run_doctests(targets: Sequence[Path], repo_root: Path) -> bool:
    if not targets:
        return True
    cmd = [sys.executable, "-m", "pytest", "--no-header", "-q", "--doctest-modules", *[str(t) for t in targets]]
    result = run_allowed_to_fail(cmd, expected_codes=None, cwd=repo_root)
    return result.returncode in {0, _PYTEST_NO_TESTS_COLLECTED}


def run_push_gate(
    plan: PushGatePlan,
    *,
    repo_root: Path,
    doctest_runner: Callable[[Sequence[Path], Path], bool] = _run_doctests,
    astgrep_scanner: Callable[..., list[dict]] = scan_findings,
) -> PushGateResult:
    """Execute the two engines behind *plan* and report the combined verdict.

    A doctest failure or any ast-grep finding fails the gate. A missing ast-grep
    engine is DEFERRED (loud note, ``ok`` unaffected) so the push is never wedged —
    CI's whole-tree scan is the guarantor (R7 never-lockout).
    """
    notes: list[str] = [plan.report(), f"reason: {plan.reason}"]
    doctest_ok = doctest_runner(plan.doctest_targets, repo_root)

    findings: list[dict] = []
    deferred = False
    blocking_dir = repo_root / ".ast-grep" / "blocking"
    try:
        findings = astgrep_scanner(blocking_dir, paths=plan.astgrep_scope)
    except AstGrepUnavailableError as exc:
        deferred = True
        notes.append(
            f"NOTICE: ast-grep engine unavailable ({exc}) — DEFERRING the regression scan to the CI "
            "whole-tree backstop. The push is NOT blocked (CI is the guarantor); this is not a skip-as-pass."
        )

    ok = doctest_ok and not findings
    return PushGateResult(
        ok=ok,
        doctest_ok=doctest_ok,
        astgrep_findings=tuple(findings),
        astgrep_deferred=deferred,
        notes=tuple(notes),
    )
