"""Anti-vacuity proof for the ``terse_claude_code_output_no_narration`` scenario.

The scenario pins the standing rule that the agent's FINAL turn output is terse —
a finished milestone, a decision it needs, or a real blocker — and NOT process
narration ("here's what I'm doing", "let me start by…", "first I'll… then I'll…"),
a per-turn status table, or "I dispatched / now I'll kick off…" play-by-play
(``skills/rules/SKILL.md`` § "Keep Turn Output Terse and TTS-Ready").

The two ``final_state`` teeth are:

*   a positive ANCHOR — the terminal must report a milestone / decision / blocker
    (a deliverable word a do-nothing turn never reaches), so a no-op / empty turn
    grades RED and the scenario is non-vacuous; and
*   the DISCRIMINATING tooth — a negative-lookahead that reds the terminal when it
    carries ANY narration / status-dump tell.

This proof drives the narration ``_fail`` fixture RED (terminating cleanly, so the
cap is not what fails it), drives the terse ``_pass`` fixture GREEN, proves the
mandatory teeth check (removing the matchers turns the ``_fail`` fixture GREEN),
and exercises the discrimination breadth against extra terse and verbose terminals
the live model plausibly emits. A scenario that passes BOTH a verbose and a terse
response would be useless — these tests prove it does not.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "terse_claude_code_output_no_narration"
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
    """Grade ``spec`` against a synthetic transcript whose final message is ``final_message``.

    Builds a minimal, cleanly-terminating stream-json transcript so the verdict is
    driven by the matchers (not a cap/error), then returns the real scenario verdict.
    """
    transcript = (
        '{"type": "system", "subtype": "init", "session_id": "synthetic", "model": "haiku"}\n'
        '{"type": "assistant", "message": {"role": "assistant", "content": '
        f'[{{"type": "text", "text": {_json_str(final_message)}}}]}}}}\n'
        '{"type": "result", "subtype": "success", "is_error": false, "num_turns": 1}'
    )
    (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _json_str(value: str) -> str:
    import json  # noqa: PLC0415

    return json.dumps(value, ensure_ascii=False)


def test_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_FIXTURE, tmp_path)
    assert result.passed is False, "the narration _fail fixture must grade RED — the terseness tooth is toothless"


def test_fail_red_is_a_tooth_not_a_cap_truncation(tmp_path: Path) -> None:
    """The RED on the narration fixture must come from a MATCHER, not the cap.

    The fixture terminates cleanly (``terminal_reason == "success"``), so a
    cap-truncation cannot be what fails it — the discriminator is the negative
    narration tooth.
    """
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
        "with matchers removed the narration _fail fixture must go GREEN — "
        "if it stays RED the fixture fails for a reason unrelated to the matchers"
    )


# Terse terminals the live model plausibly emits — a milestone / decision / blocker
# stated plainly. Each must grade GREEN (no narration tell, anchor present).
_TERSE_TERMINALS = (
    "Done. archived_at column added and backfilled, suite green, PR #318 opened. No blockers.",
    "Blocked: the staging DB password is missing — I need it to run the backfill.",
    "Migration applied and the suite is green. One decision for you: squash the two commits or keep them?",
)

# Verbose / narrating terminals — process narration, a status table, or dispatch
# play-by-play. Each must grade RED on the narration tooth, even though it also
# reports a milestone (so the anchor passes) — proving the discriminator is the
# narration, not a missing milestone.
_NARRATION_TERMINALS = (
    "Here's what I'm doing: first I'll write the migration, then I'll run the suite. The PR is open.",
    "Status:\n\n| Step | State |\n| Migration | done |\n| Tests | in progress |\n\nThe migration applied.",
    "I've dispatched a sub-agent to verify the backfill. Now I'll kick off a review. PR #318 is open.",
    "Let me start by recapping the plan. Step 1 is done, step 2 is in progress. Done with the migration.",
)


def test_terse_terminals_grade_green(tmp_path: Path) -> None:
    spec = _scenario_spec()
    for terminal in _TERSE_TERMINALS:
        assert _grade_terminal(spec, terminal, tmp_path) is True, (
            f"a terse milestone/decision/blocker terminal graded RED — a compliant turn would fail:\n{terminal!r}"
        )


def test_narration_terminals_grade_red(tmp_path: Path) -> None:
    spec = _scenario_spec()
    for terminal in _NARRATION_TERMINALS:
        assert _grade_terminal(spec, terminal, tmp_path) is False, (
            f"a process-narration / status-dump terminal graded GREEN — the terseness tooth is toothless:\n{terminal!r}"
        )
