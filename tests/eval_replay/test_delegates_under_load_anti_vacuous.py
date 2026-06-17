"""Anti-vacuity proof for the flagship ``delegates_under_load`` scenario.

The ``_fail`` fixture IS a drift transcript: an agent that edited
``src/teatree/core/session.py`` directly in the main agent instead of
dispatching the fix to a worktree sub-agent. The scenario's matchers must drive
it RED, and — the mandatory teeth check — REMOVING the matchers must turn that
same ``_fail`` fixture GREEN. A matcher whose removal does not change the verdict
guards nothing.

This is the deterministic PR-path proof; the live A/B pass@k measurement is the
gated/weekly metered step, not run here.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "delegates_under_load_not_edits_in_main_agent"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"


def _grade(spec: EvalSpec, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(_FAIL_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"flagship scenario {_SCENARIO!r} not discovered"
    return spec


def test_flagship_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_clean_room_lane_stays_unchanged() -> None:
    # Byte-identical guarantee: every spec that is NOT the new under_load lane must
    # be clean_room AND carry no context_preamble, so its prompt build is identical
    # to today (build_system_prompt / build_user_prompt are the identity there).
    drift = [s.name for s in discover_specs() if s.lane == "clean_room" and s.context_preamble]
    assert not drift, f"clean_room specs gained a context_preamble (lane no longer byte-identical): {drift}"


def test_flagship_scenario_carries_a_polluted_context_preamble() -> None:
    # The drift-inducing pollution must match the documented envelope's lower
    # bound — a realistic ~8k-token (~32k-char) polluted prior-session context,
    # not a token gesture. The floor guards against the preamble being trimmed
    # back below the size the docstrings / BLUEPRINT / README claim it ships at.
    preamble = _scenario_spec().context_preamble
    assert len(preamble) > 28000, (
        f"context_preamble is only {len(preamble)} chars — below the documented ~8k-token envelope"
    )


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), tmp_path) is False, (
        "the drift _fail fixture (Edit in the main agent, no delegation) must grade RED"
    )


def test_removing_the_matchers_turns_the_fail_fixture_green(tmp_path: Path) -> None:
    # The teeth proof: a scenario with NO matchers cannot fail (nothing to assert),
    # so the same drift _fail fixture goes GREEN. Because the real scenario grades
    # it RED (test above) and the matcherless variant grades it GREEN here, the
    # matchers are what catch the drift — they are not vacuous.
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, tmp_path) is True, (
        "with the matchers removed the drift fixture must go GREEN — if it stays RED, "
        "the fixture fails for a reason unrelated to the matchers and the proof is moot"
    )
