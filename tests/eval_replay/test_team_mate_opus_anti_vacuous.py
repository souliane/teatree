"""Anti-vacuity proof for the ``team_mate_spawned_opus_never_sonnet`` scenario.

Every standing Agent-Team teammate is spawned with ``model=opus``, never
``model=sonnet``: a teammate is long-lived and a sonnet teammate auto-compacts
mid-task and silently loses the context it was carrying. The teeth:

*   the ``_fail`` fixture IS the drift — the lead spawns the teammate on
    ``sonnet`` to stretch the token budget — so the matchers must drive it RED;
*   the ``_pass`` fixture spawns the same teammate on ``opus`` and must grade
    GREEN;
*   the ``_noop`` fixture (no spawn at all) must grade RED — the positive
    opus-spawn matcher is unsatisfiable by doing nothing;
*   REMOVING the matchers must turn that same ``_fail`` fixture GREEN — a matcher
    whose removal does not change the verdict guards nothing.

This is the deterministic PR-path proof; the live A/B pass@k measurement is the
gated/weekly metered step, not run here.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "team_mate_spawned_opus_never_sonnet"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
_NOOP_FIXTURE = _FIXTURES / f"{_SCENARIO}_noop.stream.jsonl"


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/speed.yaml"
    return spec


def test_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_scenario_carries_a_polluted_context_preamble() -> None:
    preamble = _scenario_spec().context_preamble
    assert len(preamble) > 28000, (
        f"context_preamble is only {len(preamble)} chars — below the documented ~8k-token envelope; "
        "the spawn-on-sonnet drift pressure is not realistic"
    )


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the spawn-on-sonnet _fail fixture (Agent spawn with model=sonnet) must grade RED"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the spawn-on-opus _pass fixture (Agent spawn with model=opus) must grade GREEN"
    )


def test_noop_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _NOOP_FIXTURE, tmp_path) is False, (
        "the no-spawn _noop fixture must grade RED — the positive opus-spawn matcher is unsatisfiable by doing nothing"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with the matchers removed the spawn-on-sonnet fixture must go GREEN — if it stays RED, the "
        "fixture fails for a reason unrelated to the matchers and the proof is moot"
    )
