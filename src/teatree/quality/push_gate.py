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

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.changed_set import ChangedSet, ChangedSetError, changed_paths, classify
from teatree.quality.regression_scan import AstGrepUnavailableError, scan_findings
from teatree.utils.django_db.runner import runner_prefix
from teatree.utils.run import run_allowed_to_fail

# The whole-tree doctest target — the FULL branch's ``--doctest-modules`` argument,
# byte-identical to the pre-#122 hook's ``--doctest-modules src/teatree``.
WHOLE_TREE_DOCTEST = Path("src/teatree")

_FLAG_OFF_REASON = "incremental_push_gate is OFF — whole-tree doctest + whole-tree ast-grep (the pre-#122 behaviour)"

# pytest's EXIT_NOTESTSCOLLECTED. A doctest target with no ``>>>`` example collects
# nothing and pytest exits 5 — teatree is near-zero-comments, so most modules have
# no doctests. That is NOT a doctest failure (only exit 1 is); the gate must pass.
_PYTEST_NO_TESTS_COLLECTED = 5

# Enough of a failing sweep's tail to name the offending module and its error, short
# enough that the gate's verdict stays readable.
DOCTEST_FAILURE_TAIL_LINES = 40

_SETTINGS_MODULE_ENV = "DJANGO_SETTINGS_MODULE"


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
class DoctestOutcome:
    """What the ``--doctest-modules`` sweep did — verdict, exit code, and its own output.

    The gate reports the sweep to a human, so the run's own words have to survive it
    (#3808): a bare ``ok`` reduces a real collection error to an exit code the
    operator cannot tell from an environmental flake.
    """

    ok: bool
    returncode: int
    output: str


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


def pytest_prefix(repo_root: Path) -> list[str]:
    """The pytest command prefix that runs from *repo_root*'s own environment.

    ``sys.executable`` was WRONG here: under ``t3``'s ``uv tool install`` venv (the
    local dev default) that interpreter has no pytest, so the gate exit-1'd on every
    diff (#3205). Routing through :func:`runner_prefix` runs *repo_root*'s interpreter
    (which carries pytest) and keeps the pipenv-vs-uv detection in its one chokepoint
    (#1973), never a hand-rolled second ``uv run python`` prefix here.
    """
    return [*runner_prefix(repo_root), "-m", "pytest"]


def sweep_env() -> dict[str, str]:
    """This process's environment minus ``DJANGO_SETTINGS_MODULE`` (#3808).

    The gate reads its own feature flag through the sanctioned ``django.setup()``
    entry point, whose ``setdefault`` leaves ``DJANGO_SETTINGS_MODULE`` in
    ``os.environ`` — so an inherited environment is never the caller's shell alone.
    pytest-django ranks that variable ABOVE the ini, so an inherited value silently
    swaps the settings module out from under the sweep and it collects the tree under
    application settings CI never uses. Dropping it hands the choice back to the ini,
    the same strip :func:`teatree.cli.overlay._base_env` and
    :func:`teatree.loop.dogfood_smoke._clean_subprocess_env` do for their children.
    """
    return {key: value for key, value in os.environ.items() if key != _SETTINGS_MODULE_ENV}


def sweep_argv(repo_root: Path, targets: Sequence[Path]) -> list[str]:
    """The sweep's argv — the one list :func:`_run_doctests` runs and ``--emit-cmd`` prints."""
    return [*pytest_prefix(repo_root), "--no-header", "-q", "--doctest-modules", *[str(t) for t in targets]]


def sweep_command(repo_root: Path, targets: Sequence[Path]) -> list[str]:
    """:func:`sweep_argv` with :func:`sweep_env`'s strip made visible, for pasting into a shell.

    ``--emit-cmd`` exists so the operator can re-run a failing sweep by hand, so it
    has to carry the strip too — otherwise the reproduction diverges from the run it
    claims to reproduce, in exactly the direction that manufactures a phantom failure.
    """
    return ["env", f"-u{_SETTINGS_MODULE_ENV}", *sweep_argv(repo_root, targets)]


def _run_doctests(targets: Sequence[Path], repo_root: Path) -> DoctestOutcome:
    if not targets:
        return DoctestOutcome(ok=True, returncode=0, output="")
    result = run_allowed_to_fail(sweep_argv(repo_root, targets), expected_codes=None, cwd=repo_root, env=sweep_env())
    streams = [stream for stream in (result.stdout, result.stderr) if stream]
    return DoctestOutcome(
        ok=result.returncode in {0, _PYTEST_NO_TESTS_COLLECTED},
        returncode=result.returncode,
        output="\n".join(stream.rstrip("\n") for stream in streams),
    )


def _sweep_failure_note(sweep: DoctestOutcome, targets: Sequence[Path]) -> str:
    """Say what the sweep did, so an exit 1 is never mistaken for an environmental flake.

    A gate that fails with no diagnostic is worse than no gate: it is unactionable,
    indistinguishable from a flake, and it teaches its users to bypass it (#3808).
    """
    scope = " ".join(str(target) for target in targets)
    tail = sweep.output.rstrip().splitlines()[-DOCTEST_FAILURE_TAIL_LINES:]
    detail = f"Its last {len(tail)} output line(s):\n" + "\n".join(tail) if tail else "It printed nothing."
    return f"FAILED: the doctest sweep over {scope} exited {sweep.returncode}. {detail}"


def run_push_gate(
    plan: PushGatePlan,
    *,
    repo_root: Path,
    doctest_runner: Callable[[Sequence[Path], Path], DoctestOutcome] = _run_doctests,
    astgrep_scanner: Callable[..., list[dict]] = scan_findings,
) -> PushGateResult:
    """Execute the two engines behind *plan* and report the combined verdict.

    A doctest failure or any ast-grep finding fails the gate. A missing ast-grep
    engine is DEFERRED (loud note, ``ok`` unaffected) so the push is never wedged —
    CI's whole-tree scan is the guarantor (R7 never-lockout).
    """
    notes: list[str] = [plan.report(), f"reason: {plan.reason}"]
    sweep = doctest_runner(plan.doctest_targets, repo_root)
    if not sweep.ok:
        notes.append(_sweep_failure_note(sweep, plan.doctest_targets))

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

    ok = sweep.ok and not findings
    return PushGateResult(
        ok=ok,
        doctest_ok=sweep.ok,
        astgrep_findings=tuple(findings),
        astgrep_deferred=deferred,
        notes=tuple(notes),
    )
