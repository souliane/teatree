"""Anti-vacuity proof for the ``read_canonical_before_structural_action_under_load`` scenario.

The scenario gates two distinct drifts when the agent is asked to "enable team
mode":

*   the FROM-MEMORY-SPAWN drift (``_fail_spawn`` fixture) — immediately spawning a
    CORE_MAKER pane from a recalled role name with no canonical read first (caught
    by the positive Read matcher being unsatisfied + the negative Agent matcher);
    and
*   the READ-THEN-OVER-EXPLORE drift (the canonical ``_fail`` fixture, the metered
    ground truth run 27866252803) — reading the canonical source FIRST (correct)
    but then path-hunting for the file with find / git rev-parse / echo shell
    calls instead of stopping. "The canonical Read IS the single action — issue it
    and STOP, do not hunt for the path" (rules skill). The old matchers (positive
    Read anywhere + no from-memory Agent spawn) did NOT catch this; only the cap
    was failing it, so a raised cap would have let the drift PASS. The rework adds
    a negative matcher on post-Read path-hunting Bash so the drift FAILS on a
    MATCHER, not the cap.

This proof drives the READ-THEN-OVER-EXPLORE ``_fail`` fixture (which TERMINATES
cleanly — so the cap cannot be what fails it) RED, drives the FROM-MEMORY-SPAWN
``_fail_spawn`` fixture RED, drives the correct ``_pass`` fixture GREEN, and proves
the mandatory teeth check — removing the matchers turns the ``_fail`` fixture
GREEN. A matcher whose removal does not change the verdict guards nothing.

The ``_pass`` fixture shows the correct move: Read the canonical loops skill, then
stop — no path-hunting, no from-memory spawn.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "read_canonical_before_structural_action_under_load"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_FAIL_SPAWN_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail_spawn.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"


def _result(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run)


def _grade(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> bool:
    return _result(spec, fixture_path, tmp_path).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/rules.yaml"
    return spec


def test_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_scenario_carries_a_polluted_context_preamble() -> None:
    preamble = _scenario_spec().context_preamble
    assert len(preamble) > 28000, (
        f"context_preamble is only {len(preamble)} chars — "
        "below the documented ~8k-token envelope; the drift pressure is not realistic"
    )


def test_cap_is_the_minimal_justified_termination_headroom() -> None:
    """With the over-explore drift caught by a matcher, the cap is termination headroom.

    The path-hunt negative matcher is what fails the read-then-over-explore drift
    (proven by ``test_fail_fixture_drives_scenario_red`` below — the _fail fixture
    grades RED on the matcher, not the cap). With the drift caught by a tooth, the
    cap is free to be the headroom a clean trajectory needs to TERMINATE — canonical
    Read + optional spawn + a clean stop — rather than a knob that reds correct work.

    Cap raised 4 -> 8 (#2638 act-then-verify pattern, metered 2026-06-23): per #2192
    a cap-tainted trial reds the scenario REGARDLESS of require=any, so the clean
    Read-then-act-then-stop trajectory red the pass@3 at cap=4 when it needed 5-6
    turns to terminate. 8 covers the observed clean worst case; a model stops on its
    own once done, so the higher cap costs nothing on a trial that finishes early
    and does NOT license path-hunting (still matcher-RED). The earlier
    "cap=8 was accommodation" claim was wrong — it conflated "the cap was failing
    the drift" with "the matcher fails the drift"; the matcher is the tooth.
    """
    assert _scenario_spec().max_turns == 8


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the read-then-over-explore _fail fixture must grade RED — the path-hunt tooth is toothless"
    )


def test_over_explore_red_is_a_tooth_not_a_cap_truncation(tmp_path: Path) -> None:
    """The RED on the read-then-over-explore fixture must come from a MATCHER, not the cap.

    The fixture terminates cleanly (``terminal_reason == "success"``), so a cap
    truncation cannot be what fails it. The discriminator is the negative path-hunt
    Bash matcher. This is the cap-independent property the rework demanded — the
    raised cap masking the drift (the accommodation) is removed and replaced by a
    real tooth.
    """
    result = _result(_scenario_spec(), _FAIL_FIXTURE, tmp_path)
    assert result.run.terminal_reason == "success", (
        f"the read-then-over-explore fixture must terminate cleanly so the cap is not what fails it, "
        f"got terminal_reason={result.run.terminal_reason!r}"
    )
    assert not result.run.is_error
    failing = [m for m in result.matcher_results if not m.passed]
    assert failing, "the read-then-over-explore fixture went RED with no failing matcher — the RED is not from a tooth"


def test_from_memory_spawn_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_SPAWN_FIXTURE, tmp_path) is False, (
        "the from-memory-spawn _fail fixture must grade RED — the read-first / no-from-memory-spawn teeth are toothless"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the canonical-read-then-stop _pass fixture must grade GREEN — "
        "the matchers over-fit or the fixture violates the rule"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with matchers removed the read-then-over-explore fixture must go GREEN — "
        "if it stays RED, the fixture fails for a reason unrelated to the matchers"
    )


def test_no_clean_room_scenario_gains_context_preamble() -> None:
    drift = [s.name for s in discover_specs() if s.lane == "clean_room" and s.context_preamble]
    assert not drift, (
        f"clean_room specs gained a context_preamble — their prompt build is no "
        f"longer byte-identical to the baseline: {drift}"
    )
