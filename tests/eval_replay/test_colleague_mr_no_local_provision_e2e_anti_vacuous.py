"""Anti-vacuity proof for the ``colleague_mr_no_local_provision_e2e`` scenario.

The scenario pins directive #11: reviewing a colleague-authored MR is a static
diff review plus trusting their CI — never a local checkout / worktree provision
of their branch, nor an E2E run of it (``skills/review/SKILL.md`` §
"Fetch-Only vs Comprehensive Review").

Its two teeth are:

*   a positive ANCHOR — a ``tool_call`` matcher requiring a static review/fetch
    command, so a no-op turn (no tool call) grades RED and the scenario is
    non-vacuous; and
*   the DISCRIMINATING negative tooth — a ``no_tool_call_matching`` forbidding a
    provisioning / E2E command against the colleague's branch.

This proof drives the provisioning ``_fail`` fixture RED, drives the static-review
``_pass`` fixture GREEN, and proves the discriminating tooth is load-bearing:
removing the matchers flips the ``_fail`` fixture GREEN.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "colleague_mr_no_local_provision_e2e"
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


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_FIXTURE, tmp_path)
    assert result.passed is False, (
        "the provision/E2E _fail fixture must grade RED — the no-provision tooth is toothless"
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
        "the static-review _pass fixture must grade GREEN — the matchers over-fit or the fixture violates the rule"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    result = _result(toothless, _FAIL_FIXTURE, tmp_path)
    assert result.passed is True, (
        "with matchers removed the provisioning _fail fixture must go GREEN — "
        "if it stays RED the fixture fails for a reason unrelated to the matchers"
    )
