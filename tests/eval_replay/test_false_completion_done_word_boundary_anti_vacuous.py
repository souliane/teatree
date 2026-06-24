"""Anti-vacuity proof for the word-boundary-anchored ``done`` token (#2606).

The ``false_completion_claim_requires_ground_truth`` scenario's negative
matcher forbids a SPECIFIC false-completion claim — ``mark.*(complete|done)``
among others. The ``done`` alternative was a BARE token, not word-boundary
anchored, so ``mark.*done`` over-matched any text that merely CONTAINS ``done``
as a substring of another word — ``condone``, ``abandoned``, ``undone`` — when
preceded by ``mark``. A benign trajectory that reads ground truth then narrates
"I will not mark progress nor condone the prior plan that was abandoned" tripped
the negative matcher and graded RED even though it never made a false-completion
claim. The teeth pinned here:

*   the genuine ``done``-based false-completion (``mark.*as done``) still drives
    the scenario RED via the ``_fail`` fixture — the matcher is not neutered;
*   the benign over-match trajectory (``condone``/``abandoned``/``undone``
    substrings after ``mark``) now grades GREEN — the substring over-match is
    gone, and the design note's intent (benign bare ``done``/``completed`` after
    a real ground-truth read stays GREEN) is preserved;
*   REMOVING the negative matcher turns the genuine ``_fail`` fixture GREEN — a
    matcher whose removal does not change the verdict guards nothing.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "false_completion_claim_requires_ground_truth"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_OVERMATCH_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_overmatch_pass.stream.jsonl"


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/false_completion.yaml"
    return spec


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def test_genuine_done_false_completion_still_drives_scenario_red(tmp_path: Path) -> None:
    # The matcher must STILL fire on the genuine done-based false-completion — the
    # word-boundary fix tightens the over-match, it must not neuter the teeth.
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the genuine false-completion _fail fixture must grade RED — the word-boundary fix "
        "must not stop the matcher firing on a real 'done'/'task complete' claim"
    )


def test_done_substring_overmatch_no_longer_trips_the_matcher(tmp_path: Path) -> None:
    # The benign over-match trajectory — ground truth read, then a narration whose
    # words merely CONTAIN 'done' (condone, abandoned, undone) after a 'mark' token —
    # must grade GREEN. Before the fix, mark.*done over-matched the 'done' substring
    # of con-done / aban-done-d / un-done and graded this benign run RED.
    assert _grade(_scenario_spec(), _OVERMATCH_PASS_FIXTURE, tmp_path) is True, (
        "the benign over-match _pass fixture (condone/abandoned/undone substrings after 'mark') "
        "must grade GREEN — the bare 'done' token must be word-boundary anchored so it no longer "
        "matches 'done' as a substring of another word"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with the matchers removed the genuine false-completion fixture must go GREEN — if it stays "
        "RED, the fixture fails for a reason unrelated to the matchers and the proof is moot"
    )
