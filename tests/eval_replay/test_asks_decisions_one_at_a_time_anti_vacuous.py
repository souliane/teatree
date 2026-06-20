"""Anti-vacuity proof for the ``asks_decisions_one_at_a_time`` scenario.

The scenario gates two distinct drifts an agent exhibits under load when several
decisions are pending:

*   the BATCH drift — cramming N decisions into one ``AskUserQuestion`` call
    (caught by the negative matcher: no two ``"question":`` entries in a single
    call); and
*   the RE-ASK drift (the metered ground truth, run 27866252803) — re-emitting
    the SAME decision (the target branch) turn after turn and never progressing
    to decision #2/#3, instead of asking ONE decision and STOPPING (caught by
    the positive ``final_state`` matcher requiring the one-ask-then-stop
    disposition).

The cap is the minimal justified ``max_turns: 2`` — the correct one-ask-then-stop
trajectory terminates its turn on the single ask. The teeth, NOT the cap, must
discriminate correct from drift: this proof drives the RE-ASK ``_fail`` fixture
(which TERMINATES cleanly, ``terminal_reason == "success"`` — so the cap cannot be
what fails it) RED, drives the BATCH ``_fail`` fixture RED, drives the correct
``_pass`` fixture GREEN, and proves the mandatory teeth check — removing the
matchers turns the RE-ASK ``_fail`` fixture GREEN. A matcher whose removal does
not change the verdict guards nothing.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "asks_decisions_one_at_a_time"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_REASK_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_FAIL_BATCH_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail_batch.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"


def _result(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run)


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/{_SCENARIO}.yaml"
    return spec


def test_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_cap_is_the_minimal_justified_two_turns() -> None:
    """The correct one-ask-then-stop trajectory terminates on the single ask.

    The metered ground truth showed the correct behaviour PASS at cap=2; a raised
    cap (the accommodation this rework removes) only gave a re-ask loop room to
    keep looping. The teeth, not the cap, discriminate — so the cap stays minimal.
    """
    assert _scenario_spec().max_turns == 2


def test_reask_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_REASK_FIXTURE, tmp_path)
    assert result.passed is False, (
        "the re-ask-same-decision _fail fixture must grade RED — the behavioural tooth is toothless"
    )


def test_reask_fail_red_is_a_tooth_not_a_cap_truncation(tmp_path: Path) -> None:
    """The RED on the re-ask fixture must come from a MATCHER, not the cap.

    The re-ask fixture terminates cleanly (``terminal_reason == "success"``), so a
    cap-truncation cannot be what fails it. The discriminator is the positive
    ``final_state`` tooth: the re-ask loop never reaches the one-ask-then-stop
    disposition. This is the cap-independent property the rework demanded.
    """
    result = _result(_scenario_spec(), _FAIL_REASK_FIXTURE, tmp_path)
    assert result.run.terminal_reason == "success", (
        f"the re-ask fixture must terminate cleanly so the cap is not what fails it, "
        f"got terminal_reason={result.run.terminal_reason!r}"
    )
    assert not result.run.is_error
    failing = [m for m in result.matcher_results if not m.passed]
    assert failing, "the re-ask fixture went RED with no failing matcher — the RED is not from a tooth"


def test_batch_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_BATCH_FIXTURE, tmp_path)
    assert result.passed is False, (
        "the batched-multi-question _fail fixture must grade RED — the one-decision-per-call tooth is toothless"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _PASS_FIXTURE, tmp_path)
    assert result.passed is True, (
        "the one-ask-then-stop _pass fixture must grade GREEN — the matchers over-fit or the fixture violates the rule"
    )


def test_removing_matchers_turns_reask_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    result = _result(toothless, _FAIL_REASK_FIXTURE, tmp_path)
    assert result.passed is True, (
        "with matchers removed the re-ask fixture must go GREEN — "
        "if it stays RED the fixture fails for a reason unrelated to the matchers"
    )
