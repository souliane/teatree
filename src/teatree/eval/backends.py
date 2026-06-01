"""Pluggable execution backends for the behavioral eval harness.

The eval harness grades an :class:`~teatree.eval.models.EvalRun` regardless of
HOW the run was produced — the matchers only see captured tool calls and text
blocks. That makes the *execution* swappable, which matters after the
2026-06-15 billing change: a ``claude -p`` invocation is metered.

Two backends, one ``EvalRunner`` protocol.

``ClaudePRunner`` (``backend="sdk"``) is the automated path, reserved for the
CI eval job. CI exports ``ANTHROPIC_API_KEY`` so the metered ``claude -p`` /
Agent-SDK spend is the accepted, budgeted CI cost (capped per-invocation by
``--max-budget-usd``).

``SubscriptionTranscriptRunner`` (``backend="subscription"``) is the LOCAL /
manual path that stays on the subscription. A standalone ``t3 eval run``
process has no in-session ``Agent`` tool, so it cannot itself drive a
subscription-covered model turn (see the note below). Instead the operator
runs each scenario prompt via an in-session sub-agent (subscription-covered)
with ``--output-format stream-json``, saves the transcript, and this backend
ingests it through the SAME stream-json extractors the SDK path uses — so
grading is identical.

Why no fully-automatic local-subscription backend: subscription coverage is a
property of an interactive Claude Code session driving an ``Agent`` sub-agent.
The eval CLI is a plain Python process; for it to spend subscription tokens it
would have to BE an in-session sub-agent. The interactive ``transcript``
ingest is the clean seam — the operator (or an in-session ``/loop`` driver)
produces the transcript on the subscription, the harness grades it offline.
"""

from pathlib import Path
from typing import Protocol

from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.runner import ClaudePRunner
from teatree.eval.transcript import extract_terminal_reason, extract_text_blocks, extract_tool_calls, parse_stream_json

SDK_BACKEND = "sdk"
SUBSCRIPTION_BACKEND = "subscription"
KNOWN_BACKENDS = (SDK_BACKEND, SUBSCRIPTION_BACKEND)


class EvalRunner(Protocol):
    """Anything that turns an :class:`EvalSpec` into an :class:`EvalRun`."""

    def run(self, spec: EvalSpec) -> EvalRun: ...


class UnknownBackendError(ValueError):
    """Raised for a ``--backend`` value outside :data:`KNOWN_BACKENDS`."""


def make_runner(
    backend: str,
    *,
    max_turns_override: int | None = None,
    transcript_dir: Path | None = None,
) -> EvalRunner:
    """Build the eval runner for *backend*.

    ``"sdk"`` → the metered ``claude -p`` runner (CI, ``ANTHROPIC_API_KEY``).
    ``"subscription"`` → the transcript-ingest runner (local, subscription).
    """
    if backend == SDK_BACKEND:
        return ClaudePRunner(max_turns_override=max_turns_override)
    if backend == SUBSCRIPTION_BACKEND:
        return SubscriptionTranscriptRunner(transcript_dir=transcript_dir or Path.cwd())
    msg = f"unknown eval backend {backend!r}; expected one of {', '.join(KNOWN_BACKENDS)}"
    raise UnknownBackendError(msg)


class SubscriptionTranscriptRunner:
    """Grade a scenario from a subscription-produced stream-json transcript.

    The operator runs the scenario prompt via an in-session sub-agent with
    ``--output-format stream-json`` and saves it to ``<transcript_dir>/<spec.name>.jsonl``
    (the path :meth:`transcript_path` reports). This backend reads that file
    and parses it with the identical extractors the SDK backend feeds the
    grader, so a scenario passes/fails the same way on either backend.

    A missing transcript yields a skip-shaped :class:`EvalRun` (terminal
    reason names the expected path) so a partial local run reports cleanly
    rather than erroring — symmetric with the SDK runner's missing-``claude``
    skip.
    """

    def __init__(self, *, transcript_dir: Path) -> None:
        self._transcript_dir = transcript_dir

    def transcript_path(self, spec: EvalSpec) -> Path:
        return self._transcript_dir / f"{spec.name}.jsonl"

    def run(self, spec: EvalSpec) -> EvalRun:
        path = self.transcript_path(spec)
        if not path.is_file():
            return EvalRun(
                spec_name=spec.name,
                tool_calls=(),
                text_blocks=(),
                terminal_reason=f"skipped: no subscription transcript at {path}",
                is_error=False,
                raw_stdout="",
                raw_stderr="",
            )
        raw = path.read_text(encoding="utf-8", errors="replace")
        events = parse_stream_json(raw)
        terminal_reason, is_error = extract_terminal_reason(events)
        return EvalRun(
            spec_name=spec.name,
            tool_calls=tuple(extract_tool_calls(events)),
            text_blocks=tuple(extract_text_blocks(events)),
            terminal_reason=terminal_reason,
            is_error=is_error,
            raw_stdout=raw,
            raw_stderr="",
        )
