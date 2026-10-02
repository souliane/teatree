"""Typed JSON serialization for eval matcher verdicts."""

import dataclasses
import json

from teatree.eval.models import (
    AnyOf,
    AssistantTextMatcher,
    ExpectItem,
    FinalStateMatcher,
    Matcher,
    PlanBeforeToolMatcher,
    SuccessfulToolCallMatcher,
)


@dataclasses.dataclass(frozen=True)
class MatcherJson:
    """One matcher serialized for the JSON report.

    A single matcher fills ``tool``/``arg_path``/``operator``/``value``; an
    ``any_of`` disjunction leaves them ``None`` and lists its positive
    branches under ``alternatives`` instead.
    """

    kind: str
    passed: bool
    message: str
    tool: str | None = None
    arg_path: str | None = None
    operator: str | None = None
    value: str | None = None
    result_operator: str | None = None
    result_value: str | None = None
    before_tool: str | None = None
    before_arg_path: str | None = None
    before_operator: str | None = None
    before_value: str | None = None
    alternatives: tuple["MatcherJson", ...] = ()

    @classmethod
    def of_matcher(cls, matcher: Matcher, *, passed: bool = True, message: str = "") -> "MatcherJson":
        return cls(
            kind=matcher.kind,
            tool=matcher.tool,
            arg_path=matcher.arg_path,
            operator=matcher.operator,
            value=matcher.value,
            passed=passed,
            message=message,
        )

    @classmethod
    def of_result(cls, matcher: ExpectItem, *, passed: bool, message: str) -> "MatcherJson":
        if isinstance(matcher, AnyOf):
            return cls(
                kind="any_of",
                passed=passed,
                message=message,
                alternatives=tuple(cls.of_matcher(alt) for alt in matcher.alternatives),
            )
        if isinstance(matcher, FinalStateMatcher | AssistantTextMatcher):
            return cls(
                kind="final_state" if isinstance(matcher, FinalStateMatcher) else "assistant_text",
                operator=matcher.operator,
                value=matcher.value,
                passed=passed,
                message=message,
            )
        if isinstance(matcher, PlanBeforeToolMatcher):
            return cls(
                kind="assistant_text_before_first_tool",
                tool=" | ".join(matcher.governed_tools),
                value=json.dumps(matcher.patterns),
                passed=passed,
                message=message,
            )
        if isinstance(matcher, SuccessfulToolCallMatcher):
            return cls(
                kind="tool_call_succeeded",
                tool=matcher.tool,
                arg_path=matcher.arg_path,
                operator=matcher.operator,
                value=matcher.value,
                result_operator=matcher.result_operator,
                result_value=matcher.result_value,
                before_tool=matcher.before_tool,
                before_arg_path=matcher.before_arg_path,
                before_operator=matcher.before_operator,
                before_value=matcher.before_value,
                passed=passed,
                message=message,
            )
        return cls.of_matcher(matcher, passed=passed, message=message)


def matcher_json_dict(matcher: MatcherJson) -> dict[str, str | bool | list[object]]:
    """Serialize a :class:`MatcherJson`, omitting unset (``None``) scalar keys.

    A single matcher emits its ``tool``/``arg_path``/``operator``/``value``;
    an ``any_of`` omits those and emits ``alternatives`` instead.
    """
    out: dict[str, str | bool | list[object]] = {"kind": matcher.kind}
    for key in (
        "tool",
        "arg_path",
        "operator",
        "value",
        "result_operator",
        "result_value",
        "before_tool",
        "before_arg_path",
        "before_operator",
        "before_value",
    ):
        scalar = getattr(matcher, key)
        if scalar is not None:
            out[key] = scalar
    if matcher.alternatives:
        out["alternatives"] = [matcher_json_dict(alt) for alt in matcher.alternatives]
    out["passed"] = matcher.passed
    out["message"] = matcher.message
    return out
