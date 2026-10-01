"""Anti-vacuity proof for the post-merge-obligation rubric scenario.

``rubric_criteria_exclude_post_merge_obligations`` pins the merge deadlock: a
criterion bundling "CI green" with a close-after-merge and a post-deploy
transcript check can never PASS at the head, so the merge gate refuses forever.

*   the ``_fail`` fixture IS that bundle, so the scenario grades RED;
*   removing ONLY the negative matcher turns it GREEN, so the negative is what
    catches it and not an unrelated positive;
*   the ``_drop_fail`` fixture dodges the negative by dropping the obligations
    entirely, so it must grade RED too; and
*   the ``_pass`` fixture, carrying both obligations as post-merge plan steps,
    grades GREEN — the negative is bounded to the ``acceptance_criteria`` object.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate

_SCENARIO = "rubric_criteria_exclude_post_merge_obligations"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _fixture(kind: str) -> Path:
    return _FIXTURES / f"{_SCENARIO}_{kind}.stream.jsonl"


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered"
    return spec


def _without_the_negative(spec: EvalSpec) -> EvalSpec:
    kept = tuple(m for m in spec.matchers if not (isinstance(m, Matcher) and m.kind == "negative"))
    assert len(kept) == len(spec.matchers) - 1, "expected exactly one negative matcher to remove"
    return dataclasses.replace(spec, matchers=kept)


def test_a_post_merge_obligation_bundled_into_a_criterion_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _fixture("fail"), tmp_path) is False, (
        "the _fail fixture bundles '#4891 closed after merge' into a criterion — it must grade RED"
    )


def test_removing_the_negative_turns_the_fail_fixture_green(tmp_path: Path) -> None:
    assert _grade(_without_the_negative(_scenario_spec()), _fixture("fail"), tmp_path) is True, (
        "with the negative removed the bundle fixture must go GREEN — if it stays RED, a positive "
        "is failing it and the negative guards nothing"
    )


def test_dropping_the_obligations_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _fixture("drop_fail"), tmp_path) is False, (
        "dropping #4891 and the transcript check dodges the negative — it must grade RED, since the "
        "rule moves a post-merge obligation to the plan's post-merge steps, never deletes it"
    )


def test_obligations_in_the_post_merge_steps_drive_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _fixture("pass"), tmp_path) is True, (
        "head-provable criteria plus post-merge steps is the compliant plan and must grade GREEN"
    )
