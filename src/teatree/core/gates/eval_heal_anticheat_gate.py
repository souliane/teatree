"""Anti-cheat structural gate for the CI-eval self-healing loop (#3201 PR-2, #4220).

The invariant this gate enforces is non-negotiable: a behavioral eval red must be
FIXED, never suppressed. The healer's fix diff may touch the *product* — skill
prose, hooks, core code — the levers that actually change agent behaviour. It may
NEVER touch the *test*: the scenario definitions (``evals/scenarios/**``) or the
eval harness that grades them (``src/teatree/eval/**``). Editing either would turn
a red green without changing behaviour — a suppressed red masquerading as a pass.

Both bans are DEFAULT-DENY prefixes, which is the #4220 fix. A hand-listed set of
four graders (``matchers``/``triage``/``judge``/``matcher_vacuity``) admitted
``report.py`` — the module computing ``ScenarioResult.passed`` — and every other
grading module by omission, so a fixer could widen the verdict itself and pass the
gate. A prefix cannot drift away from a surface it does not enumerate;
``EVAL_HARNESS_ALLOWED_PATHS`` carries the exceptions, and
``tests/teatree_core/gates/test_eval_heal_anticheat_gate.py`` refuses an entry
that sits on the computed grading call graph (:mod:`teatree.quality.eval_grading_surface`).

The gate is a pure structural decision over the set of changed paths (from ``git
diff --name-only``), so it is deterministic and testable with no git/network. It
is wired into ``CiEvalHealSession.record_fix`` via the gate registry (the model
fetches it by name at call time, keeping the model → gate edge inverted like the
forced-repro gate), and raises :class:`EvalHealCheatError` — an
:class:`InvalidTransitionError` subclass — so a cheating fix rolls the transition
back and the session stays in ``FIXING`` rather than reaching ``PUSHED``.
"""

from collections.abc import Iterable
from pathlib import PurePosixPath

from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.errors import InvalidTransitionError

#: The scenario tree — the behavioral-eval *tests* themselves. Any changed path
#: under this prefix is a forbidden edit (the healer would be rewriting the test).
SCENARIO_DIR_PREFIX = "evals/scenarios/"

#: The eval harness — everything that loads, runs, grades, or reports a scenario.
#: Denied wholesale rather than enumerated: no heal fix has ever needed to edit
#: the harness, and a red the harness itself caused is a human decision (halt and
#: escalate), so default-deny costs nothing and closes the omission class.
EVAL_HARNESS_PREFIX = "src/teatree/eval/"

#: The reviewed exemptions from :data:`EVAL_HARNESS_PREFIX`. Empty by design — an
#: entry is a deliberate decision, and the conformance test refuses one that lies
#: on the grading call graph.
EVAL_HARNESS_ALLOWED_PATHS: frozenset[str] = frozenset()


class EvalHealCheatError(InvalidTransitionError):
    """A heal fix was refused: it edits the scenario tree or the eval harness.

    A subclass of :class:`InvalidTransitionError` so a ``record_fix`` that hits it
    rolls the FSM advance back and the session stays in ``FIXING``. The message
    names every forbidden path and restates the fix-the-code-not-the-test rule.
    """


def _under(normalized: str, prefix: str) -> bool:
    """Whether *normalized* sits under *prefix* at ANY tree depth.

    Matched on a path-SEGMENT boundary rather than anchored at the repo root: a repo that
    VENDORS this package under a subdirectory reports these very files with a prefix, so a
    root-anchored match would leave the healer free to rewrite the test there.
    """
    return normalized.startswith(prefix) or f"/{prefix}" in normalized


def _is_forbidden(path: str) -> bool:
    """True when *path* is a scenario definition or a red matcher, at ANY tree depth.

    Matched on a path-SEGMENT boundary rather than anchored at the repo root: a
    repo that VENDORS this package under a subdirectory reports these very files
    with a prefix, so a root-anchored match would leave the healer free to rewrite
    the test there — the one thing this gate exists to refuse.
    """
    normalized = str(PurePosixPath(path)) if path not in {"", "."} else path
    normalized = normalized.removeprefix("./")
    if _under(normalized, SCENARIO_DIR_PREFIX):
        return True
    return _under(normalized, EVAL_HARNESS_PREFIX) and not any(
        normalized == allowed or normalized.endswith(f"/{allowed}") for allowed in EVAL_HARNESS_ALLOWED_PATHS
    )


def classify_fix_diff(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Return, in input order, the changed paths a fix diff may not touch.

    Empty tuple means the diff is clean (product code only). A non-empty tuple is
    the set of scenario-tree / eval-harness paths that make the fix a cheat.
    """
    return tuple(path for path in changed_paths if _is_forbidden(path))


def _deny_message(forbidden: tuple[str, ...]) -> str:
    listed = "\n".join(f"    - {path}" for path in forbidden)
    return (
        "Refusing this heal fix — it touches the eval TEST, not the code. A behavioral eval red must be "
        "FIXED by changing the product (skill prose, hooks, core code), never by editing the scenario "
        "definitions or the eval harness that grades them. Forbidden paths in this diff:\n"
        f"{listed}\n"
        "Revert those edits and fix the behaviour the scenario asserts. If the scenario itself is wrong, "
        "that is a human decision — halt and escalate, do not self-edit the test."
    )


def assert_fix_touches_only_code(changed_paths: Iterable[str]) -> None:
    """Raise :class:`EvalHealCheatError` if the fix diff touches a forbidden path."""
    forbidden = classify_fix_diff(changed_paths)
    if forbidden:
        raise EvalHealCheatError(_deny_message(forbidden))


register_gate("eval_heal_anticheat", assert_fix_touches_only_code)
