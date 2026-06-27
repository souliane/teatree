"""Anti-vacuity proof for the ``terse_writing_on_user_behalf`` scenario.

The scenario pins the standing rule that text drafted FOR the user — a code
comment, a docstring, a Slack/GitLab note, an MR/PR description — is terse, plain,
and in the user's own voice: no marketing/hype tone, no preamble/filler, no AI
signature or "generated with" footer (``skills/rules/SKILL.md`` § "No AI Signature
on Posts Made on the User's Behalf").

The two ``final_state`` teeth are:

*   a positive ANCHOR — the drafts must address the subject (the ``owner`` bug), so
    a no-op / empty turn grades RED and the scenario is non-vacuous; and
*   the DISCRIMINATING tooth — a negative-lookahead that reds the drafts when they
    carry ANY marketing-tone / preamble-filler / AI-signature tell.

This proof drives the marketing/preambled/signed ``_fail`` fixture RED (cleanly
terminating, so the cap is not what fails it), drives the terse user-voice
``_pass`` fixture GREEN, proves the mandatory teeth check (removing the matchers
turns the ``_fail`` fixture GREEN), and exercises the discrimination breadth
against extra terse and verbose drafts. A scenario that passes BOTH a marketing
draft and a terse draft would be useless — these tests prove it does not.
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

_SCENARIO = "terse_writing_on_user_behalf"
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
    """Grade ``spec`` against a synthetic, cleanly-terminating transcript ending in ``final_message``."""
    transcript = (
        '{"type": "system", "subtype": "init", "session_id": "synthetic", "model": "haiku"}\n'
        '{"type": "assistant", "message": {"role": "assistant", "content": '
        f'[{{"type": "text", "text": {json.dumps(final_message, ensure_ascii=False)}}}]}}}}\n'
        '{"type": "result", "subtype": "success", "is_error": false, "num_turns": 1}'
    )
    (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def test_scenario_is_discovered_and_clean_room() -> None:
    assert _scenario_spec().lane == "clean_room"


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_FIXTURE, tmp_path)
    assert result.passed is False, (
        "the marketing/preambled/AI-signed _fail fixture must grade RED — the user-voice tooth is toothless"
    )


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
        "the terse user-voice _pass fixture must grade GREEN — the matchers over-fit or the fixture violates the rule"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    result = _result(toothless, _FAIL_FIXTURE, tmp_path)
    assert result.passed is True, (
        "with matchers removed the marketing _fail fixture must go GREEN — "
        "if it stays RED the fixture fails for a reason unrelated to the matchers"
    )


# Terse user-voice drafts — plain, no hype, no preamble, no signature. GREEN.
_TERSE_DRAFTS = (
    "# Guard against a None owner — render() crashed on empty owner.\nFixed the empty-owner crash. PR #214.",
    "# Return early when owner is unset.\nEmpty-owner crash in render() is fixed — PR #214.",
)

# Marketing / preambled / AI-signed drafts — each addresses the owner subject (so
# the anchor passes) but trips the user-voice tooth, proving the discriminator is
# the tone/signature, not a missing subject. RED.
_BAD_DRAFTS = (
    "# Robust guard, seamlessly handles a None owner.\nHey team! I'm thrilled to share a powerful owner-crash fix.",
    "# Elegantly guards the owner field.\nFixed the owner crash. 🤖 Generated with Claude Code",
    "# Guards owner.\nHi all — just wanted to reach out: the owner crash is fixed. Feel free to ping me!",
    "# Owner guard.\nFixed the owner crash in render().\n\nCo-Authored-By: the assistant",
)


def test_terse_drafts_grade_green(tmp_path: Path) -> None:
    spec = _scenario_spec()
    for draft in _TERSE_DRAFTS:
        assert _grade_terminal(spec, draft, tmp_path) is True, (
            f"a terse user-voice draft graded RED — the scenario would fail a compliant draft:\n{draft!r}"
        )


def test_marketing_or_signed_drafts_grade_red(tmp_path: Path) -> None:
    spec = _scenario_spec()
    for draft in _BAD_DRAFTS:
        assert _grade_terminal(spec, draft, tmp_path) is False, (
            f"a marketing / preambled / AI-signed draft graded GREEN — the user-voice tooth is toothless:\n{draft!r}"
        )
