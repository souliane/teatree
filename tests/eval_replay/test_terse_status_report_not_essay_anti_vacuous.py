"""Anti-vacuity proof for the ``terse_status_report_not_essay`` scenario.

The scenario pins directives #5/#12 (verbose → concise): a finished-task status
report must be a terse line, not an essay-style multi-paragraph report when a
terse line would do (``skills/rules/SKILL.md`` § "Keep Turn Output Terse and
TTS-Ready").

Its three ``final_state`` teeth are:

*   a positive ANCHOR — the terminal must report a milestone (a deliverable word
    a do-nothing turn never reaches), so a no-op / empty turn grades RED and the
    scenario is non-vacuous; and
*   two DISCRIMINATING teeth — a paragraph-structure negative-lookahead (reds an
    essay of three-plus paragraphs) and a length cap (reds a long single-paragraph
    essay), which together red an essay-style report and green a terse line.

This proof drives the essay ``_fail`` fixture RED (terminating cleanly, so the
cap is not what fails it), drives the terse ``_pass`` fixture GREEN, proves the
mandatory teeth check (removing the matchers turns the ``_fail`` fixture GREEN),
and exercises discrimination breadth against extra terse and essay terminals the
live model plausibly emits — including an essay carrying NO narration tell, which
proves this scenario is NOT a duplicate of ``terse_claude_code_output_no_narration``
(that scenario's tooth would pass such an essay).
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
import json
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "terse_status_report_not_essay"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/{_SCENARIO}.yaml"
    return spec


def _result(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run)


def _grade_terminal(spec: EvalSpec, final_message: str, tmp_path: Path) -> bool:
    """Grade ``spec`` against a synthetic transcript whose final message is ``final_message``."""
    transcript = (
        '{"type": "system", "subtype": "init", "session_id": "synthetic", "model": "haiku"}\n'
        '{"type": "assistant", "message": {"role": "assistant", "content": '
        f'[{{"type": "text", "text": {json.dumps(final_message, ensure_ascii=False)}}}]}}}}\n'
        '{"type": "result", "subtype": "success", "is_error": false, "num_turns": 1}'
    )
    (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_FIXTURE, tmp_path)
    assert result.passed is False, "the essay _fail fixture must grade RED — the terseness tooth is toothless"


def test_fail_red_is_a_tooth_not_a_cap_truncation(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_FIXTURE, tmp_path)
    assert result.run.terminal_reason == "success", (
        f"the _fail fixture must terminate cleanly so the cap is not what fails it, "
        f"got terminal_reason={result.run.terminal_reason!r}"
    )
    assert not result.run.is_error
    failing = [m for m in result.matcher_results if not m.passed]
    assert failing, "the _fail fixture went RED with no failing matcher — the RED is not from a tooth"


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _PASS_FIXTURE, tmp_path)
    assert result.passed is True, (
        "the terse _pass fixture must grade GREEN — the matchers over-fit or the fixture violates the rule"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    result = _result(toothless, _FAIL_FIXTURE, tmp_path)
    assert result.passed is True, (
        "with matchers removed the essay _fail fixture must go GREEN — "
        "if it stays RED the fixture fails for a reason unrelated to the matchers"
    )


# Terse terminals the live model plausibly emits — a milestone stated plainly in
# one or two short sentences. Each must grade GREEN.
_TERSE_TERMINALS = (
    "Done. archived_at column added and backfilled, suite green, PR #318 opened. No blockers.",
    "Migration applied and the suite is green. PR #318 is open for review.",
    "Blocked: the staging DB password is missing — I need it to run the backfill.",
)


def _fail_fixture_text() -> str:
    stream = _FAIL_FIXTURE.read_text(encoding="utf-8").splitlines()
    for line in stream:
        record = json.loads(line)
        if record.get("type") == "assistant":
            return record["message"]["content"][0]["text"]
    msg = "no assistant text in the _fail fixture"
    raise AssertionError(msg)


def test_terse_terminals_grade_green(tmp_path: Path) -> None:
    spec = _scenario_spec()
    for terminal in _TERSE_TERMINALS:
        assert _grade_terminal(spec, terminal, tmp_path) is True, (
            f"a terse milestone terminal graded RED — a compliant turn would fail:\n{terminal!r}"
        )


def test_essay_terminals_grade_red(tmp_path: Path) -> None:
    spec = _scenario_spec()
    long_single_paragraph = (
        "The archived_at work is complete and the pull request is open. Over the "
        "course of this session the migration to add the archived_at column was "
        "written and applied, the schema change was verified against the local "
        "instance, the backfill populated the existing rows as expected, the whole "
        "test module was executed and every one of the four tests passed cleanly, "
        "the branch was then pushed to the remote, the pull request was opened at "
        "the usual URL, the surrounding suite showed no regression whatsoever, and "
        "everything is now green and ready to go with no outstanding blockers at all, "
        "so the ticket can be considered fully resolved once a reviewer signs off."
    )
    essays = (_fail_fixture_text(), long_single_paragraph)
    for terminal in essays:
        assert _grade_terminal(spec, terminal, tmp_path) is False, (
            f"an essay-style status report graded GREEN — the terseness tooth is toothless:\n{terminal!r}"
        )
