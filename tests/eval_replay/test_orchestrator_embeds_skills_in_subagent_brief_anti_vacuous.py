r"""Anti-vacuity proof for the whole-preamble sub-agent brief matcher (#4769).

The scenario asked only for ONE ``--- SKILL:`` block, so a brief carrying the
``rules`` body but silently dropping the requested ``e2e`` body graded GREEN —
the dropped-skill shape the issue is about. The matchers now require every
requested skill. Pinned here:

*   the partial-preamble fixture grades RED under the scenario, and GREEN under
    the old single-occurrence matchers — the tightening is what catches it;
*   the full-preamble ``_pass`` fixture stays GREEN;
*   each matcher alone rejects the partial brief, and with neither it grades GREEN.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "orchestrator_embeds_skills_in_subagent_brief"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"

_SINGLE_OCCURRENCE_EXPECT = """
-   name: orchestrator_embeds_skills_in_subagent_brief
    agent_path: skills/rules/references/sub-agents.md
    scenario: the pre-#4769 matchers
    prompt: unused
    expect:
        -   any_of:
                -   tool_call: Agent
                    args.prompt: '~ "(?s)--- SKILL: "'
                -   tool_call: Bash
                    args.command: '~ "t3 \\S+ skill-preamble"'
        -   no_tool_call_matching:
                Agent.prompt: '~ "(?s)\\A(?!.*--- SKILL:).+"'
"""


def _spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered"
    return spec


def _single_occurrence_spec(tmp_path: Path) -> EvalSpec:
    path = tmp_path / "old.yaml"
    path.write_text(_SINGLE_OCCURRENCE_EXPECT, encoding="utf-8")
    (old,) = load_eval_yaml(path)
    return dataclasses.replace(_spec(), matchers=old.matchers)


def _passes(spec: EvalSpec, kind: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{_SCENARIO}_{kind}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


def test_a_brief_that_drops_a_requested_skill_grades_red(tmp_path: Path) -> None:
    assert _passes(_spec(), "partial_fail", tmp_path) is False


def test_the_single_occurrence_matchers_let_the_partial_brief_pass(tmp_path: Path) -> None:
    old = _single_occurrence_spec(tmp_path)
    assert _passes(dataclasses.replace(old, matchers=old.matchers[1:]), "fail", tmp_path) is False, (
        "the reconstructed pre-#4769 tooth must still reject a bare brief, or this comparison is vacuous"
    )
    assert _passes(old, "partial_fail", tmp_path) is True


def test_the_full_preamble_brief_still_grades_green(tmp_path: Path) -> None:
    assert _passes(_spec(), "pass", tmp_path) is True


def test_the_negative_matcher_alone_rejects_the_partial_brief(tmp_path: Path) -> None:
    spec = _spec()
    anchor_only = dataclasses.replace(spec, matchers=spec.matchers[:1])
    tooth_only = dataclasses.replace(spec, matchers=spec.matchers[1:])
    assert _passes(anchor_only, "partial_fail", tmp_path) is False
    assert _passes(tooth_only, "partial_fail", tmp_path) is False
    assert _passes(dataclasses.replace(spec, matchers=()), "partial_fail", tmp_path) is True
