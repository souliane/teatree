"""Grade a captured session against its ground-truth :class:`CorpusLabel`.

Three pieces:

*   :func:`captured_run` adapts the parsed on-disk :class:`SessionEvent` stream
    (see :mod:`teatree.eval.session_transcript`) into the :class:`EvalRun` shape
    the grader consumes — the same bridge :mod:`teatree.eval.subagent_transcript`
    builds for a sub-agent JSONL, here over the richer session envelope.
*   :func:`grade` synthesizes a throwaway :class:`EvalSpec` from the label's
    matchers/judge and delegates to :func:`teatree.eval.report.evaluate`, so a
    corpus entry grades through the exact same code path as a scenario.
*   :func:`assert_independent_oracle` is the anti-circular guard: a
    matcher-graded label whose human/external labeller is the same identity as
    the rule's author is a circular oracle (the label cannot disagree with the
    rule), and is refused. A judge/``both`` oracle or a genuinely distinct
    labeller passes.
"""

from pathlib import Path
from typing import Any, cast

from teatree.eval.corpus_models import CorpusLabel
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall
from teatree.eval.report import JudgeGrader, ScenarioResult, evaluate
from teatree.eval.session_transcript import SessionEvent

_DIRTY_STOP_REASONS = frozenset({"max_tokens", "refusal", "error", "aborted"})

_SYNTHETIC_AGENT_PATH = "corpus"
_SYNTHETIC_SOURCE = "corpus"


class CircularOracleError(ValueError):
    """A matcher-graded label whose labeller is also the rule's author."""


def captured_run(label: CorpusLabel, events: list[SessionEvent]) -> EvalRun:
    """Assemble an :class:`EvalRun` from a parsed session capture.

    The run's tool calls are the session's ``tool_use`` events in order; the
    terminal reason is the last assistant turn's ``stop_reason`` — absent or
    non-string is a clean ``"completed"``, only an explicitly dirty reason
    (``max_tokens`` / ``refusal`` / ``error`` / ``aborted``) is an error.
    """
    terminal_reason, is_error = _terminal_reason(events)
    return EvalRun(
        spec_name=label.entry_id,
        tool_calls=_tool_calls(events),
        text_blocks=tuple(_text_blocks(events)),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
    )


def _tool_calls(events: list[SessionEvent]) -> tuple[EvalToolCall, ...]:
    calls: list[EvalToolCall] = []
    for event in events:
        name = event.tool_name
        if name is None:
            continue
        calls.append(EvalToolCall(name=name, input=event.tool_input or {}, turn=len(calls) + 1))
    return tuple(calls)


def grade(label: CorpusLabel, events: list[SessionEvent], *, judge: JudgeGrader | None = None) -> ScenarioResult:
    """Grade a captured session against *label* via :func:`report.evaluate`."""
    spec = _synthesize_spec(label)
    return evaluate(spec, captured_run(label, events), judge=judge)


def assert_independent_oracle(label: CorpusLabel, *, judge_present: bool = False) -> None:
    """Refuse a matcher-only label graded by the same identity that authored the rule.

    A label graded ONLY by matchers whose ground-truth labeller equals the rule's
    author is a circular oracle (the label cannot disagree with the rule). The
    independent grader that breaks the circle is the LLM judge — so the refusal
    applies to a ``matcher`` oracle AND to a ``both`` oracle graded with no judge
    present (the no-judge default grades ``both`` matcher-only, so the ``both``
    label is exactly as circular as a ``matcher`` one). A ``both`` oracle WITH a
    judge, a pure ``judge`` oracle, and a distinct (or absent) author all pass.
    """
    matcher_only = label.oracle == "matcher" or (label.oracle == "both" and not judge_present)
    if not matcher_only or not label.rule_author:
        return
    if _strip_role(label.labelled_by) == _strip_role(label.rule_author):
        msg = (
            f"corpus entry {label.entry_id!r}: matcher oracle labelled by "
            f"{label.labelled_by!r}, the same identity as the rule author "
            f"{label.rule_author!r} — a circular oracle"
        )
        raise CircularOracleError(msg)


def _strip_role(identity: str) -> str:
    return identity.split(":", 1)[-1].strip()


def _synthesize_spec(label: CorpusLabel) -> EvalSpec:
    return EvalSpec(
        name=label.entry_id,
        scenario=label.expected_behavior,
        agent_path=_SYNTHETIC_AGENT_PATH,
        prompt=label.expected_behavior,
        matchers=label.matchers,
        source_path=Path(_SYNTHETIC_SOURCE),
        judge=label.judge,
    )


def _terminal_reason(events: list[SessionEvent]) -> tuple[str, bool]:
    for event in reversed(events):
        if event.type != "assistant":
            continue
        message = event.raw.get("message")
        stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
        if not isinstance(stop_reason, str):
            return "completed", False
        return stop_reason, stop_reason in _DIRTY_STOP_REASONS
    return "aborted", True


def _text_blocks(events: list[SessionEvent]) -> list[str]:
    blocks: list[str] = []
    seen_lines: set[int] = set()
    for event in events:
        if event.type != "assistant" or event.line_no in seen_lines:
            continue
        seen_lines.add(event.line_no)
        message = event.raw.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        blocks.extend(text for item in content if (text := _block_text(item)) is not None)
    return blocks


def _block_text(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    block = cast("dict[str, Any]", item)
    if block.get("type") != "text":
        return None
    text = block.get("text")
    return text if isinstance(text, str) else None
