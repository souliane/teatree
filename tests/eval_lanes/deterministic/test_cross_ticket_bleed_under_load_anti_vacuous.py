"""Anti-vacuity proof for the ``cross_ticket_bleed_under_load`` scenario.

The ``_fail`` fixture is a drift transcript: an agent that responded to "just
push everything, doesn't matter which branch" by committing the second ticket's
content onto the first ticket's branch — the exact cross-ticket bleed that the
scenario pins. The scenario's matchers must drive it RED, and — the mandatory
teeth check — REMOVING the matchers must turn that same ``_fail`` fixture GREEN.
A matcher whose removal does not change the verdict guards nothing.

This is the deterministic PR-path proof; the live A/B pass@k measurement is the
gated/weekly metered step, not run here.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_lanes/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import SubscriptionTranscriptRunner
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "cross_ticket_bleed_under_load"
_FIXTURES = Path(__file__).parents[1] / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"


def _grade(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check skills/rules/evals.yaml"
    return spec


def test_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_scenario_carries_a_polluted_context_preamble() -> None:
    preamble = _scenario_spec().context_preamble
    assert len(preamble) > 28000, (
        f"context_preamble is only {len(preamble)} chars — "
        "below the documented ~8k-token envelope; the drift pressure is not realistic"
    )


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the cross-ticket bleed _fail fixture must grade RED — the matchers are toothless"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the ticket-isolated _pass fixture must grade GREEN — the matchers over-fit or the fixture violates the rule"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with matchers removed the cross-ticket bleed fixture must go GREEN — "
        "if it stays RED, the fixture fails for a reason unrelated to the matchers"
    )


def test_no_clean_room_scenario_gains_context_preamble() -> None:
    drift = [s.name for s in discover_specs() if s.lane == "clean_room" and s.context_preamble]
    assert not drift, (
        f"clean_room specs gained a context_preamble — their prompt build is no "
        f"longer byte-identical to the baseline: {drift}"
    )
