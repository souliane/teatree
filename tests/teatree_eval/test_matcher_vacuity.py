"""Structural guard: a negative matcher must be paired with a positive anchor.

A behavioral scenario graded ONLY by negative matchers
(``no_tool_call_matching``) is vacuously satisfied by a no-op agent — an agent
that does nothing trivially never makes the forbidden tool call, so the scenario
reads green while guarding nothing (#2441). The existing ``_noop`` runtime gate
(``test_scenarios_anti_vacuous.py``) catches this by replaying every spec against
a no-op transcript; this module is the *structural* sibling that makes the
vacuous shape a fast, fixture-free RED — a negative matcher with no positive
anchor in the same ``expect`` list is flagged at load time.

A "positive anchor" is any matcher that forces the agent to *do* something the
no-op cannot satisfy: a positive :class:`~teatree.eval.models.Matcher`, an
:class:`~teatree.eval.models.AnyOf` (a disjunction of positive alternatives), or
a :class:`~teatree.eval.models.FinalStateMatcher` (which requires the final
assistant message to carry specific content).
"""

from pathlib import Path

from teatree.eval.matcher_vacuity import (
    has_negative_matcher,
    has_positive_anchor,
    is_negative_only,
    is_positive_anchor,
    negative_only_specs,
)
from teatree.eval.models import AnyOf, EvalSpec, FinalStateMatcher, Matcher

_POSITIVE = Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="git push")
_NEGATIVE = Matcher(kind="negative", tool="Bash", arg_path="command", operator="~", value="--no-verify")
_ANY_OF = AnyOf(alternatives=(_POSITIVE,))
_FINAL = FinalStateMatcher(operator="~", value="(?i)done")


def _spec(*matchers: object) -> EvalSpec:
    return EvalSpec(
        name="__synthetic__",
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="synthetic",
        matchers=tuple(matchers),
        source_path=Path("synthetic.yaml"),
    )


class TestIsPositiveAnchor:
    def test_positive_matcher_is_an_anchor(self) -> None:
        assert is_positive_anchor(_POSITIVE) is True

    def test_negative_matcher_is_not_an_anchor(self) -> None:
        assert is_positive_anchor(_NEGATIVE) is False

    def test_any_of_is_an_anchor(self) -> None:
        assert is_positive_anchor(_ANY_OF) is True

    def test_final_state_matcher_is_an_anchor(self) -> None:
        assert is_positive_anchor(_FINAL) is True


class TestHasPositiveAnchor:
    def test_positive_plus_negative_pair_has_an_anchor(self) -> None:
        assert has_positive_anchor(_spec(_POSITIVE, _NEGATIVE)) is True

    def test_negative_only_has_no_anchor(self) -> None:
        assert has_positive_anchor(_spec(_NEGATIVE)) is False

    def test_two_negatives_only_has_no_anchor(self) -> None:
        assert has_positive_anchor(_spec(_NEGATIVE, _NEGATIVE)) is False

    def test_any_of_paired_with_negative_has_an_anchor(self) -> None:
        assert has_positive_anchor(_spec(_ANY_OF, _NEGATIVE)) is True

    def test_final_state_paired_with_negative_has_an_anchor(self) -> None:
        assert has_positive_anchor(_spec(_FINAL, _NEGATIVE)) is True

    def test_matcherless_judge_only_spec_has_no_anchor_but_is_not_negative_only(self) -> None:
        # A matcherless (judge-only) spec carries no negative matcher, so it is
        # exempt from the pairing rule — ``negative_only_specs`` must not flag it.
        spec = _spec()
        assert has_positive_anchor(spec) is False
        assert spec not in negative_only_specs([spec])


class TestHasNegativeMatcher:
    def test_negative_matcher_is_detected(self) -> None:
        assert has_negative_matcher(_spec(_NEGATIVE)) is True

    def test_positive_only_has_no_negative_matcher(self) -> None:
        assert has_negative_matcher(_spec(_POSITIVE)) is False

    def test_anchors_alone_are_not_negative_matchers(self) -> None:
        # any_of / final_state are positive anchors, never negative matchers.
        assert has_negative_matcher(_spec(_ANY_OF, _FINAL)) is False


class TestIsNegativeOnly:
    def test_negative_only_spec_is_negative_only(self) -> None:
        assert is_negative_only(_spec(_NEGATIVE)) is True

    def test_paired_spec_is_not_negative_only(self) -> None:
        assert is_negative_only(_spec(_POSITIVE, _NEGATIVE)) is False

    def test_positive_only_spec_is_not_negative_only(self) -> None:
        assert is_negative_only(_spec(_POSITIVE)) is False

    def test_judge_only_spec_is_not_negative_only(self) -> None:
        assert is_negative_only(_spec()) is False


class TestNegativeOnlySpecs:
    def test_flags_a_negative_only_spec(self) -> None:
        offender = _spec(_NEGATIVE)
        assert offender in negative_only_specs([offender])

    def test_does_not_flag_a_paired_spec(self) -> None:
        paired = _spec(_POSITIVE, _NEGATIVE)
        assert paired not in negative_only_specs([paired])

    def test_does_not_flag_a_positive_only_spec(self) -> None:
        positive_only = _spec(_POSITIVE)
        assert positive_only not in negative_only_specs([positive_only])

    def test_does_not_flag_a_judge_only_spec(self) -> None:
        judge_only = _spec()
        assert judge_only not in negative_only_specs([judge_only])

    def test_returns_every_offender_in_a_mixed_catalog(self) -> None:
        bad_one = _spec(_NEGATIVE)
        bad_two = _spec(_NEGATIVE, _NEGATIVE)
        good = _spec(_POSITIVE, _NEGATIVE)
        offenders = negative_only_specs([bad_one, good, bad_two])
        assert bad_one in offenders
        assert bad_two in offenders
        assert good not in offenders
