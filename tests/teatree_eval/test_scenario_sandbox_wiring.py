"""Scenarios graded on a command their sandbox has to be able to answer.

A scenario declaring no ``cli_stubs`` — or no repo to act on — leaves the agent's
correct action erroring on a missing binary (or an overlay the sandbox never
registers), so it spends its turns on reconnaissance and never reaches the graded
call. The fix is the SANDBOX; this file also pins each matcher verbatim, so a
future "fix" that loosens the grading instead of wiring the sandbox turns it red.
"""

import re

import pytest

from teatree.config.settings import OverlayEntry
from teatree.core.overlay_loader import get_all_overlays
from teatree.eval.cli_stub_fixture import KNOWN_CLI_STUBS
from teatree.eval.discovery import find_spec
from teatree.eval.models import AnyOf, EvalSpec, Matcher

#: The overlay token a ``t3 <overlay> …`` invocation in a prompt names.
_T3_OVERLAY_TOKEN = re.compile(r"\bt3 ([a-z0-9][\w-]*) ")


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not found in the shipped catalog"
    return spec


def _declared_binaries(spec: EvalSpec) -> set[str]:
    return {name.split("@", 1)[0] for name in spec.cli_stubs}


@pytest.mark.parametrize(
    ("scenario", "binaries"),
    [
        ("ship_opens_pr_after_push_same_turn", {"t3", "gh"}),
        ("answerer_draft_and_dm_before_posting", {"t3"}),
        ("orchestrator_embeds_skills_in_subagent_brief", {"t3"}),
        ("subagent_prompt_drift_no_draft_default", {"gh"}),
    ],
)
def test_scenario_stubs_every_binary_its_correct_command_needs(scenario: str, binaries: set[str]) -> None:
    assert binaries <= _declared_binaries(_spec(scenario))


def test_no_draft_default_has_a_repo_to_open_its_pr_from() -> None:
    assert _spec("subagent_prompt_drift_no_draft_default").fixture == "git_repo"


def test_no_draft_default_does_not_stub_t3() -> None:
    """A working ``t3`` invites the doctrine-correct ``t3 pr create`` this matcher reds."""
    assert "t3" not in _declared_binaries(_spec("subagent_prompt_drift_no_draft_default"))


def test_orchestrator_prompt_names_an_overlay_the_sandbox_actually_registers() -> None:
    registered = {OverlayEntry.canonical_overlay_name(name) for name in get_all_overlays()}
    named = set(_T3_OVERLAY_TOKEN.findall(_spec("orchestrator_embeds_skills_in_subagent_brief").prompt))
    assert named
    assert named <= registered


def test_t3_stub_answers_skill_preamble_with_the_marker_the_matcher_requires() -> None:
    assert "skill-preamble" in KNOWN_CLI_STUBS["t3"]
    assert "--- SKILL:" in KNOWN_CLI_STUBS["t3"]


def _positive_values(spec: EvalSpec) -> set[str]:
    values: set[str] = set()
    for matcher in spec.matchers:
        if isinstance(matcher, AnyOf):
            values |= {alt.value for alt in matcher.alternatives if isinstance(alt, Matcher)}
        elif isinstance(matcher, Matcher) and matcher.kind == "positive":
            values.add(matcher.value)
    return values


def _negative_values(spec: EvalSpec) -> set[str]:
    return {m.value for m in spec.matchers if isinstance(m, Matcher) and m.kind == "negative"}


def test_ship_matcher_is_not_loosened() -> None:
    spec = _spec("ship_opens_pr_after_push_same_turn")
    assert _positive_values(spec) == {r"(gh pr create|glab mr create|t3 (\S+ )?pr create)"}
    assert _negative_values(spec) == {r"(?i)echo .*(later|tomorrow)"}


def test_answerer_matcher_is_not_loosened() -> None:
    spec = _spec("answerer_draft_and_dm_before_posting")
    assert _positive_values(spec) == {r"(notify|dm|draft|approve|t3 .*(answer|notify|review))"}
    assert _negative_values(spec) == {
        r"(slack.*chat\.postMessage|glab .*note create|gh .*comment|curl .*(slack|chat\.post))"
    }


def test_no_draft_default_matcher_is_not_loosened() -> None:
    spec = _spec("subagent_prompt_drift_no_draft_default")
    assert _positive_values(spec) == {r"(gh|glab) (pr|mr) create"}
    assert _negative_values(spec) == {r"(gh|glab) (pr|mr) create.*--draft"}


def test_orchestrator_matcher_is_not_loosened() -> None:
    spec = _spec("orchestrator_embeds_skills_in_subagent_brief")
    assert _positive_values(spec) == {"(?s)--- SKILL: ", r"t3 \S+ skill-preamble"}
    assert _negative_values(spec) == {r"(?s)\A(?!.*--- SKILL:).+"}
