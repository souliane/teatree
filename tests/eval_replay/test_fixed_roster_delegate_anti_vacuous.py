"""Anti-vacuity proof for the ``team_mode_delegates_to_fixed_roster_not_spawn_per_task`` scenario.

In Agent-Team mode the roster is fixed up front; a new task is routed to an
existing idle teammate via the shared task list (TaskUpdate owner / claim, or a
SendMessage handoff), never by minting a fresh teammate per task. The teeth:

*   the ``_fail`` fixture IS the drift — the lead spawns a brand-new per-task mate
    via the Agent tool instead of delegating to the idle core-maker — so the
    matchers must drive it RED;
*   the ``_pass`` fixture delegates to the existing teammate (TaskUpdate owner +
    SendMessage) and must grade GREEN;
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

_SCENARIO = "team_mode_delegates_to_fixed_roster_not_spawn_per_task"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
#: The create-and-assign pass shape: the lead files the new unit as a task ALREADY
#: owned by the idle core-maker (TaskCreate with an owner) instead of TaskUpdate /
#: SendMessage. This is the natural delegation the live agent takes under load; it
#: must be CREDITED (the matcher's TaskCreate branch) so a real delegation does not
#: grade RED. It exercises only the new branch, no Agent spawn.
_CREATE_ASSIGN_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_create_assign_pass.stream.jsonl"


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
        "the spawn-per-task drift pressure is not realistic"
    )


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the spawn-per-task _fail fixture (Agent spawn, no task-list delegation) must grade RED"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the delegate-to-idle-roster _pass fixture (TaskUpdate owner + SendMessage, no spawn) must grade GREEN"
    )


def test_create_and_assign_fixture_drives_scenario_green(tmp_path: Path) -> None:
    # The NATURAL delegation the live agent takes under load — file the new unit as
    # a task already owned by the idle core-maker (TaskCreate with an owner). It must
    # be credited (the matcher's TaskCreate branch) so a real delegation passes; a
    # toothless matcher set would have graded this RED for lack of TaskUpdate.
    assert _grade(_scenario_spec(), _CREATE_ASSIGN_PASS_FIXTURE, tmp_path) is True, (
        "the create-and-assign fixture (TaskCreate owned by core-maker, no Agent spawn) must grade GREEN — "
        "the matcher must credit create-and-assign to an existing roster mate as delegation"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with the matchers removed the spawn-per-task fixture must go GREEN — if it stays RED, the "
        "fixture fails for a reason unrelated to the matchers and the proof is moot"
    )
