"""Anti-vacuity proof for the merged ``review_done_reacts_with_verdict_emoji`` scenario.

Two drifts from the same finished-clean-review state used to be two scenarios
(#3566): claiming with ``:eyes:`` instead of signalling done, and DMing the MR
author instead of reacting. They graded the same positive trajectory, so they are
one scenario carrying two negatives — and each negative needs its own violating
fixture, or the merge would have quietly deleted a tooth.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "review_done_reacts_with_verdict_emoji"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_EYES_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_FAIL_AUTHOR_DM_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail_author_dm.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"


def _spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/review.yaml"
    return spec


def _result(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec))


def _without_negatives(spec: EvalSpec) -> EvalSpec:
    kept = tuple(m for m in spec.matchers if not (isinstance(m, Matcher) and m.kind == "negative"))
    return dataclasses.replace(spec, matchers=kept)


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _result(_spec(), _PASS_FIXTURE, tmp_path).passed is True


def test_eyes_claim_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _result(_spec(), _FAIL_EYES_FIXTURE, tmp_path).passed is False


def test_author_dm_fixture_drives_scenario_red(tmp_path: Path) -> None:
    """The tooth the merge inherited from the retired DM scenario."""
    assert _result(_spec(), _FAIL_AUTHOR_DM_FIXTURE, tmp_path).passed is False


def test_the_author_dm_red_comes_from_a_negative_matcher(tmp_path: Path) -> None:
    """Without the negatives the DM fixture goes GREEN — so the RED is that tooth.

    The fixture emits the CORRECT verdict reaction as well, so the positive matcher
    is satisfied and only the author-DM negative can be what fails it.
    """
    assert _result(_without_negatives(_spec()), _FAIL_AUTHOR_DM_FIXTURE, tmp_path).passed is True
