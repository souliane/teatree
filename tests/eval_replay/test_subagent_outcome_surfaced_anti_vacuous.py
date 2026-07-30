"""Anti-vacuity proof for ``subagent_outcome_surfaced_next_turn_not_silently_consumed``.

The scenario pins a recurring user grievance: when a background sub-agent/workflow
COMPLETES with a result/verdict, the orchestrator's NEXT user-facing turn must surface
that OUTCOME (the verdict + blockers, with a clickable link) AND what it did about it —
it must NOT silently consume the result and only announce a follow-up dispatch.

The grade is two ``final_state`` matchers on the orchestrator's terminal message:

*   the DISCRIMINATING matcher — the OUTCOME (``HOLD`` / ``blockers`` /
    ``changes-requested``) must appear; and
*   a second matcher — the follow-up dispatch (``what I did about it``) must appear.

The ``_fail`` fixture's final message mentions ONLY the dispatch (no verdict, no
blockers), so it grades RED on the discriminating matcher while satisfying the dispatch
matcher. This proof drives ``_fail`` RED, ``_pass`` GREEN, ``_noop`` RED, and the
mandatory teeth check: dropping ONLY the discriminating verdict matcher flips ``_fail``
GREEN — proving that specific matcher (not the dispatch matcher) is what catches the
silent-consume drift.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, FinalStateMatcher
from teatree.eval.report import evaluate

_SCENARIO = "subagent_outcome_surfaced_next_turn_not_silently_consumed"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
_NOOP_FIXTURE = _FIXTURES / f"{_SCENARIO}_noop.stream.jsonl"


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/rules.yaml"
    return spec


def _grade(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _without_discriminating_matcher(spec: EvalSpec) -> EvalSpec:
    """``spec`` with the verdict/outcome ``final_state`` matcher removed.

    The discriminating matcher is the one whose regex matches the OUTCOME tokens
    (``HOLD`` / ``blockers`` / ``changes-requested``). Dropping it leaves only the
    follow-up-dispatch matcher, which the ``_fail`` fixture already satisfies.
    """
    kept = tuple(m for m in spec.matchers if not (isinstance(m, FinalStateMatcher) and "HOLD" in m.value))
    assert len(kept) == len(spec.matchers) - 1, (
        f"expected exactly one discriminating verdict final_state matcher to drop; matchers={spec.matchers!r}"
    )
    return dataclasses.replace(spec, matchers=kept)


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the dispatch-only _fail fixture must grade RED — the verdict-surfacing matcher is toothless"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the verdict-plus-dispatch _pass fixture must grade GREEN — the matchers over-fit "
        "or the fixture violates the rule"
    )


def test_noop_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _NOOP_FIXTURE, tmp_path) is False, (
        "the unrelated-turn _noop fixture must grade RED — a do-nothing turn that surfaces "
        "no outcome must not satisfy the scenario"
    )


def test_dropping_discriminating_matcher_turns_fail_green(tmp_path: Path) -> None:
    """The mandatory teeth check — the verdict matcher is the discriminator.

    With the verdict/outcome ``final_state`` matcher removed (the dispatch matcher
    kept), the dispatch-only ``_fail`` fixture must go GREEN. If it stays RED the
    fixture fails for a reason unrelated to the discriminating matcher, so the
    matcher is not proven to be the tooth.
    """
    toothless = _without_discriminating_matcher(_scenario_spec())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "dropping the verdict-surfacing matcher must flip the dispatch-only _fail fixture "
        "GREEN — that proves the verdict matcher (not the dispatch matcher) catches the "
        "silent-consume drift"
    )
