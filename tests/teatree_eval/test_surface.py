"""The question-SURFACE axis: which question/answer surface a scenario grades."""

from pathlib import Path
from typing import Literal

from teatree.eval.models import HEADLESS_SURFACE, INTERACTIVE_SURFACE, AnyOf, EvalSpec, ExpectItem, Matcher
from teatree.eval.surface import is_advisory, mislabelled_interactive_specs, requires_interactive_tool_call


def _matcher(tool: str, kind: Literal["positive", "negative"] = "positive") -> Matcher:
    return Matcher(kind=kind, tool=tool, arg_path="", operator="contains", value="")


def _spec(name: str, *matchers: ExpectItem, surface: str = HEADLESS_SURFACE) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=matchers,
        source_path=Path("synthetic.yaml"),
        surface=surface,
    )


class TestRequiresInteractiveToolCall:
    def test_a_required_positive_askuserquestion_matcher_requires_the_tool_call(self) -> None:
        assert requires_interactive_tool_call(_spec("s", _matcher("AskUserQuestion")))

    def test_case_is_canonicalized_before_comparison(self) -> None:
        assert requires_interactive_tool_call(_spec("s", _matcher("askuserquestion")))

    def test_an_any_of_alternative_does_not_require_it(self) -> None:
        # A disjunction has another satisfiable branch, so the scenario never depends
        # on the bundled CLI rendering the call as a tool_use block.
        disjunction = AnyOf(alternatives=(_matcher("Agent"), _matcher("AskUserQuestion")))
        assert not requires_interactive_tool_call(_spec("s", disjunction))

    def test_a_negative_matcher_does_not_require_it(self) -> None:
        # "never call AskUserQuestion" is satisfied by a run that emits no chip at all.
        assert not requires_interactive_tool_call(_spec("s", _matcher("AskUserQuestion", kind="negative")))

    def test_a_bash_only_scenario_does_not_require_it(self) -> None:
        assert not requires_interactive_tool_call(_spec("s", _matcher("Bash")))


class TestAdvisory:
    def test_interactive_surface_is_advisory(self) -> None:
        assert is_advisory(_spec("s", surface=INTERACTIVE_SURFACE))

    def test_headless_surface_blocks(self) -> None:
        assert not is_advisory(_spec("s", surface=HEADLESS_SURFACE))


class TestMislabelled:
    def test_a_hard_required_tool_call_left_headless_is_mislabelled(self) -> None:
        spec = _spec("s", _matcher("AskUserQuestion"), surface=HEADLESS_SURFACE)
        assert mislabelled_interactive_specs([spec]) == [spec]

    def test_the_same_scenario_labelled_interactive_is_clean(self) -> None:
        spec = _spec("s", _matcher("AskUserQuestion"), surface=INTERACTIVE_SURFACE)
        assert mislabelled_interactive_specs([spec]) == []
