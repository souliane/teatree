"""Anti-cheat structural gate for the CI-eval self-healing loop (#3201 PR-2).

The invariant this gate enforces is non-negotiable: a behavioral eval red must be
FIXED, never suppressed. The healer's fix diff may touch the *product* — skill
prose, hooks, core code — the levers that actually change agent behaviour. It may
NEVER touch the *test*: the scenario definitions (``evals/scenarios/**``) or the
red-matcher grading machinery that decides a scenario is red. Editing either would
turn a red green without changing behaviour — a suppressed red masquerading as a
pass.

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

#: The red-matcher grading machinery — the code that decides a scenario is red.
#: Weakening any of these turns a red green without a behavioural fix:
#: ``matchers.py`` (the assertion engine), ``triage.py`` (``classify_red``),
#: ``judge.py`` (the LLM judge), ``matcher_vacuity.py`` (the anti-vacuity guard
#: whose neutering would let a vacuous matcher pass everything).
RED_MATCHER_PATHS: frozenset[str] = frozenset(
    {
        "src/teatree/eval/matchers.py",
        "src/teatree/eval/triage.py",
        "src/teatree/eval/judge.py",
        "src/teatree/eval/matcher_vacuity.py",
    }
)


class EvalHealCheatError(InvalidTransitionError):
    """A heal fix was refused: it edits the scenario tree or the red matcher.

    A subclass of :class:`InvalidTransitionError` so a ``record_fix`` that hits it
    rolls the FSM advance back and the session stays in ``FIXING``. The message
    names every forbidden path and restates the fix-the-code-not-the-test rule.
    """


def _is_forbidden(path: str) -> bool:
    normalized = str(PurePosixPath(path)) if path not in {"", "."} else path
    normalized = normalized.removeprefix("./")
    return normalized.startswith(SCENARIO_DIR_PREFIX) or normalized in RED_MATCHER_PATHS


def classify_fix_diff(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Return, in input order, the changed paths a fix diff may not touch.

    Empty tuple means the diff is clean (product code only). A non-empty tuple is
    the set of scenario-tree / red-matcher paths that make the fix a cheat.
    """
    return tuple(path for path in changed_paths if _is_forbidden(path))


def _deny_message(forbidden: tuple[str, ...]) -> str:
    listed = "\n".join(f"    - {path}" for path in forbidden)
    return (
        "Refusing this heal fix — it touches the eval TEST, not the code. A behavioral eval red must be "
        "FIXED by changing the product (skill prose, hooks, core code), never by editing the scenario "
        "definitions or the red matcher that grades them. Forbidden paths in this diff:\n"
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
