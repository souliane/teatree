"""Type-surface guards for the eval harness dataclasses."""

from typing import get_args

from teatree.eval.models import Matcher


def test_matcher_kind_is_narrowed_to_positive_or_negative() -> None:
    """``Matcher.kind`` is a closed ``positive``/``negative`` vocabulary, not open ``str``.

    Every constructor (``loader._positive_matcher`` / ``_negative_matcher``) and every
    reader (``report._dispatch``, ``matcher_vacuity``) agrees the field is exactly one of
    those two tokens; the annotation must encode that closed set so a stray third value is
    a type error at authorship instead of a silent grader no-op.
    """
    field_type = Matcher.__dataclass_fields__["kind"].type
    assert get_args(field_type) == ("positive", "negative")
