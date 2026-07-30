"""Type-surface guards for the eval harness dataclasses."""

from typing import get_args

import pytest

from teatree.eval.models import EvalRun, Matcher


def test_skipped_matches_the_old_skip_run_shape() -> None:
    """``EvalRun.skipped`` reproduces the byte-identical shape the per-runner ``_skip_run`` built.

    The three fresh-run backends each carried a private ``_skip_run`` that stamped a
    ``skipped: <reason>`` terminal reason on an empty, non-error run. The classmethod
    that collapses them must produce exactly that record.
    """
    run = EvalRun.skipped("alpha", "ANTHROPIC_API_KEY not resolvable")
    assert run == EvalRun(
        spec_name="alpha",
        tool_calls=(),
        text_blocks=(),
        terminal_reason="skipped: ANTHROPIC_API_KEY not resolvable",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def test_terminal_matches_the_old_terminal_run_shape() -> None:
    """``EvalRun.terminal`` reproduces the byte-identical shape the old ``_terminal_run`` built.

    A run that never produced a transcript (timeout / budget cap) is empty,
    ``is_error=True``, and carries the classified terminal reason; the optional
    ``cost_usd`` floors a budget-exceeded cap (``0.0`` for a timeout).
    """
    assert EvalRun.terminal("beta", terminal_reason="timeout") == EvalRun(
        spec_name="beta",
        tool_calls=(),
        text_blocks=(),
        terminal_reason="timeout",
        is_error=True,
        raw_stdout="",
        raw_stderr="",
    )
    capped = EvalRun.terminal("beta", terminal_reason="budget_exceeded", cost_usd=0.1)
    assert capped.cost_usd == pytest.approx(0.1)
    assert capped.is_error is True


def test_matcher_kind_is_narrowed_to_positive_or_negative() -> None:
    """``Matcher.kind`` is a closed ``positive``/``negative`` vocabulary, not open ``str``.

    Every constructor (``loader._positive_matcher`` / ``_negative_matcher``) and every
    reader (``report._dispatch``, ``matcher_vacuity``) agrees the field is exactly one of
    those two tokens; the annotation must encode that closed set so a stray third value is
    a type error at authorship instead of a silent grader no-op.
    """
    field_type = Matcher.__dataclass_fields__["kind"].type
    assert get_args(field_type) == ("positive", "negative")
