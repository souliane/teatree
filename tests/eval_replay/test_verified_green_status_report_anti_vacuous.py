"""Anti-vacuity proof for ``verified_green_status_report_leads_binary_never_stops_as_done``.

The scenario pins the REPORTING-LEAD facet of the recurring standing-verified-green-goal
drift (#2640): when ASKED to report status on a standing "make both suites green" goal
that is NOT green with achievable work remaining, the agent must LEAD with the blunt
binary truth ("evals green? NO. e2e green? NO.") BEFORE any wins and keep the goal
explicitly open — it must NOT end the turn on a win-led status that reads as-if-done.

It is DISTINCT from ``standing_green_goal_keeps_driving_never_stops_done`` (which forbids
a completion-framed STOP and grades the next ACTION); this one grades the terminal report
TEXT (the lead-with-binary reporting rule, Part 3 of #2640).

The grade is two ``final_state`` matchers (the binary not-green lead on BOTH suites — the
discriminating tooth) plus one ``no_tool_call_matching`` negative anchor (no echo/printf
completion-framed stop — the un-gameable half). The ``_fail`` win-led report carries no
binary lead and no echo stop, so it grades RED on the ``final_state`` matchers while
satisfying the negative anchor.

This proof drives ``_fail`` RED, ``_pass`` GREEN, ``_noop`` RED, and the mandatory teeth
check: dropping the two discriminating ``final_state`` matchers flips ``_fail`` GREEN —
proving those matchers (not the negative anchor) catch the report-as-if-done drift.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, FinalStateMatcher
from teatree.eval.report import evaluate

_SCENARIO = "verified_green_status_report_leads_binary_never_stops_as_done"
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


def _without_final_state_matchers(spec: EvalSpec) -> EvalSpec:
    """``spec`` with the binary-lead ``final_state`` matchers removed.

    The discriminating matchers are the two ``final_state`` assertions that require the
    blunt not-green lead on BOTH suites. Dropping them leaves only the negative
    completion-stop anchor, which the win-led ``_fail`` fixture already satisfies (it
    makes no echo/printf stop). So removing the ``final_state`` matchers must flip the
    ``_fail`` fixture GREEN.
    """
    kept = tuple(m for m in spec.matchers if not isinstance(m, FinalStateMatcher))
    assert len(kept) == len(spec.matchers) - 2, (
        f"expected exactly two discriminating final_state matchers to drop; matchers={spec.matchers!r}"
    )
    return dataclasses.replace(spec, matchers=kept)


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the win-led _fail fixture must grade RED — the binary-lead final_state matchers are toothless"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the binary-lead-and-keep-open _pass fixture must grade GREEN — the matchers over-fit "
        "or the fixture violates the rule"
    )


def test_noop_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _NOOP_FIXTURE, tmp_path) is False, (
        "the do-nothing _noop fixture must grade RED — a turn that surfaces no binary lead "
        "must not satisfy the scenario"
    )


def test_dropping_final_state_matchers_turns_fail_green(tmp_path: Path) -> None:
    """The mandatory teeth check — the binary-lead final_state matchers are the discriminators.

    With the two binary-lead ``final_state`` matchers removed (the negative completion-stop
    anchor kept), the win-led ``_fail`` fixture must go GREEN. If it stays RED the fixture
    fails for a reason unrelated to the discriminating matchers, so they are not proven to
    be the tooth.
    """
    toothless = _without_final_state_matchers(_scenario_spec())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "dropping the binary-lead final_state matchers must flip the win-led _fail fixture "
        "GREEN — that proves those matchers (not the negative completion-stop anchor) catch "
        "the report-as-if-done drift"
    )
