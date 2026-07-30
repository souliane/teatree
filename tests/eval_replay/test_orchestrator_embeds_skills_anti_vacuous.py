"""Anti-vacuity proof for the ``orchestrator_embeds_skills_in_subagent_brief`` scenario.

The ``_fail`` fixture is a drift transcript: an orchestrator that spawned an
e2e sub-agent through the raw Agent tool with a BARE brief — no skill preamble,
so the sub-agent inherits none of the loaded skills. The scenario's matchers
must drive it RED, and the mandatory teeth check — removing the matchers must
turn that same ``_fail`` fixture GREEN. A matcher whose removal does not change
the verdict guards nothing.

The ``_pass`` fixture shows the correct dispatch: obtain the preamble via
``t3 <overlay> skill-preamble`` and spawn the sub-agent with the embedded
``SKILL.md`` bodies prepended to the brief. The ``_noop`` fixture (no tool
calls) must also be RED — proving the positive matcher is genuinely required.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "orchestrator_embeds_skills_in_subagent_brief"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
_NOOP_FIXTURE = _FIXTURES / f"{_SCENARIO}_noop.stream.jsonl"


def _grade(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/"
    return spec


def test_scenario_targets_the_sub_agent_limitations_rule() -> None:
    spec = _scenario_spec()
    assert spec.agent_path == "skills/rules/SKILL.md"
    assert "Sub-Agent Limitations" in spec.agent_sections


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the bare-brief _fail fixture must grade RED — the matchers are toothless"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the preamble-embedded _pass fixture must grade GREEN — the matchers over-fit or the fixture violates the rule"
    )


def test_noop_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _NOOP_FIXTURE, tmp_path) is False, (
        "a no-tool-call transcript must grade RED — the positive matcher is missing, "
        "so the scenario is satisfied by a no-op orchestrator and therefore vacuous"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with matchers removed the bare-brief fixture must go GREEN — "
        "if it stays RED, the fixture fails for a reason unrelated to the matchers"
    )
