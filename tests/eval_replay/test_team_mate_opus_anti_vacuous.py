"""Anti-vacuity proof for the ``team_mate_spawned_opus_never_sonnet`` scenario.

REDESIGNED for the headless SDK lane (#2596 / #2601 decision (b)). The original
assertion graded a per-teammate ``model=opus`` argument — a HOST roster capability
the SDK eval lane cannot control or verify (it fixes the run model centrally and
has no Agent-team runtime to observe the resolved teammate tier). The model-tier
sub-assertion is DROPPED from this lane; what remains is the SDK-testable essence:
faced with a heavy standing-role unit (the deferred BLUEPRINT + README sync), the
lead DELEGATES it to a sub-agent (Agent/Task dispatch) instead of doing the heavy
doc work INLINE in the main agent. The teeth:

*   the ``_fail`` fixture IS the drift — the lead edits the BLUEPRINT itself in the
    main agent instead of delegating — so the matchers must drive it RED;
*   the ``_pass`` fixture dispatches the BLUEPRINT/README sync to a sub-agent and
    must grade GREEN;
*   the ``_delegate_to_mate_pass`` fixture hands the unit to an idle roster mate via
    ``TaskUpdate(owner=…)`` (the bundle-prescribed team-mode delegation) and must
    grade GREEN too — #37 broadened the positive any_of to credit a TaskUpdate /
    SendMessage hand-off, since the full bundle's team-mode rule actively teaches it
    and the original Agent/Task-only matcher contradicted that rule;
*   the ``_noop`` fixture (no tool call at all) must grade RED — the positive
    delegation matcher is unsatisfiable by doing nothing;
*   the model tier carried on the spawn is NOT graded (a delegating spawn passes
    regardless of its ``model`` arg) — proving the host-only sub-assertion was
    genuinely dropped, not relocated;
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
_DELEGATE_TO_MATE_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_delegate_to_mate_pass.stream.jsonl"
_NOOP_FIXTURE = _FIXTURES / f"{_SCENARIO}_noop.stream.jsonl"


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/wip.yaml"
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
        "the inline-work _fail fixture (lead edits BLUEPRINT.md in the main agent, no delegation) must grade RED"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the delegation _pass fixture (Agent dispatch of the BLUEPRINT/README sync) must grade GREEN"
    )


def test_delegate_to_mate_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    # #37: a team lead handing the standing-role unit to an idle roster mate via
    # TaskUpdate(owner=…) is the bundle-prescribed team-mode delegation — it must
    # grade GREEN now that the positive any_of credits the TaskUpdate/SendMessage
    # hand-off, not only an Agent/Task dispatch. The negative tooth (no inline
    # BLUEPRINT/README edit) is unchanged, so this broadening does not weaken the gate.
    assert _grade(_scenario_spec(), _DELEGATE_TO_MATE_PASS_FIXTURE, tmp_path) is True, (
        "the delegate-to-idle-mate _pass fixture (TaskUpdate owner=core-maker, no inline edit) must grade GREEN"
    )


def test_noop_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _NOOP_FIXTURE, tmp_path) is False, (
        "the no-tool-call _noop fixture must grade RED — the positive delegation matcher is unsatisfiable "
        "by doing nothing"
    )


def test_model_tier_on_the_spawn_is_not_graded(tmp_path: Path) -> None:
    # The host-only sub-assertion (per-teammate model tier) was DROPPED, not
    # relocated: a delegating dispatch must grade GREEN regardless of the `model` arg
    # it carries. A `model=sonnet` dispatch of the BLUEPRINT/README sync still passes
    # — if it RED'd, a model-tier check would still be hiding in the matcher.
    sonnet_dispatch = (
        '{"type": "system", "subtype": "init", "session_id": "vac-tier-not-graded", "model": "haiku"}\n'
        '{"type": "assistant", "message": {"role": "assistant", "content": ['
        '{"type": "tool_use", "id": "t1", "name": "Agent", '
        '"input": {"name": "docs-maker", "model": "sonnet", "description": "BLUEPRINT + README sync", '
        '"prompt": "Do the overdue BLUEPRINT + README sync in a fresh worktree and report back."}}]}}\n'
        '{"type": "result", "subtype": "success", "is_error": false, "num_turns": 1}\n'
    )
    spec = _scenario_spec()
    (tmp_path / f"{spec.name}.jsonl").write_text(sonnet_dispatch, encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    assert evaluate(spec, run).passed is True, (
        "a delegating dispatch carrying model=sonnet must still grade GREEN — the SDK lane does not grade the "
        "per-teammate model tier; a RED here would mean a host-only model-tier check is still hiding in the matcher"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with the matchers removed the inline-work fixture must go GREEN — if it stays RED, the "
        "fixture fails for a reason unrelated to the matchers and the proof is moot"
    )
