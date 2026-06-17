"""Structural vacuity guard: a negative matcher must have a positive anchor.

A behavioral scenario graded ONLY by negative matchers (``no_tool_call_matching``)
is vacuously satisfied by a no-op agent: a do-nothing agent trivially never makes
the forbidden tool call, so the scenario reads green while guarding nothing
(#2441). The runtime ``_noop`` gate in ``test_scenarios_anti_vacuous.py`` already
catches this by replaying every spec against a no-op transcript; this module is
the *structural* sibling that makes the vacuous shape a fast, fixture-free RED —
the offending ``expect`` list is flagged at load time, before any fixture runs.

A **positive anchor** is any matcher that forces the agent to *do* something a
no-op cannot satisfy:

*   a positive :class:`~teatree.eval.models.Matcher` (a matching tool call must
    exist),
*   an :class:`~teatree.eval.models.AnyOf` (a disjunction of positive
    alternatives — at least one matching call must exist), or
*   a :class:`~teatree.eval.models.FinalStateMatcher` (the final assistant
    message must carry specific content).

A scenario carrying at least one negative matcher and no positive anchor is the
vacuous class this guard flags. A matcherless (judge-only) scenario carries no
negative matcher, so it is exempt — there is nothing to pair.
"""

from teatree.eval.models import AnyOf, EvalSpec, ExpectItem, FinalStateMatcher, Matcher


def is_positive_anchor(matcher: ExpectItem) -> bool:
    """Whether ``matcher`` forces the agent to act (breaking no-op vacuity).

    A positive ``Matcher``, an ``AnyOf`` disjunction, and a ``FinalStateMatcher``
    each require the agent to produce something a do-nothing run cannot. A
    negative ``Matcher`` is satisfied by inaction, so it is NOT an anchor.
    """
    if isinstance(matcher, AnyOf | FinalStateMatcher):
        return True
    return isinstance(matcher, Matcher) and matcher.kind == "positive"


def has_negative_matcher(spec: EvalSpec) -> bool:
    """Whether ``spec`` carries at least one negative (``no_tool_call_matching``) matcher."""
    return any(isinstance(m, Matcher) and m.kind == "negative" for m in spec.matchers)


def has_positive_anchor(spec: EvalSpec) -> bool:
    """Whether ``spec`` has at least one positive anchor in its ``expect`` list."""
    return any(is_positive_anchor(m) for m in spec.matchers)


def is_negative_only(spec: EvalSpec) -> bool:
    """Whether ``spec`` carries a negative matcher but no positive anchor — the vacuous class."""
    return has_negative_matcher(spec) and not has_positive_anchor(spec)


def negative_only_specs(specs: list[EvalSpec]) -> list[EvalSpec]:
    """The offenders: specs with a negative matcher and no positive anchor.

    A negative-only scenario is satisfied by a no-op agent, so it guards nothing.
    The gate (``test_scenarios_anti_vacuous.py``) consumes this and fails loud,
    naming each offender, so the vacuous shape can never reach the suite green.
    """
    return [spec for spec in specs if is_negative_only(spec)]
