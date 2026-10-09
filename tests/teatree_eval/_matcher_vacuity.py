"""Test-only structural checks for matcher vacuity."""

from teatree.eval.matcher_vacuity import is_positive_anchor
from teatree.eval.models import EvalSpec, Matcher


def has_negative_matcher(spec: EvalSpec) -> bool:
    return any(isinstance(m, Matcher) and m.kind == "negative" for m in spec.matchers)


def has_positive_anchor(spec: EvalSpec) -> bool:
    return any(is_positive_anchor(m) for m in spec.matchers)


def is_negative_only(spec: EvalSpec) -> bool:
    return has_negative_matcher(spec) and not has_positive_anchor(spec)


def negative_only_specs(specs: list[EvalSpec]) -> list[EvalSpec]:
    return [spec for spec in specs if is_negative_only(spec)]
