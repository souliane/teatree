"""Pluggable execution backends for the behavioral eval harness.

The eval harness grades an :class:`~teatree.eval.models.EvalRun` regardless of
HOW the run was produced — the matchers only see captured tool calls and text
blocks. That makes the *execution* swappable, which matters after the
2026-06-15 billing change: a metered Agent-SDK invocation is billed.

Two backends, one ``EvalRunner`` protocol.

:class:`~teatree.eval.sdk_runner.SdkInProcessRunner` (``backend="sdk"``) is the
automated path, reserved for the CI eval job. It drives ``claude-agent-sdk``
in-process; the metered Agent-SDK spend is the accepted, budgeted CI cost
(capped per-invocation by ``max_budget_usd``).

``SubscriptionTranscriptRunner`` (``backend="subscription"``) is the LOCAL /
manual path that stays on the subscription. A standalone ``t3 eval run``
process has no in-session ``Agent`` tool, so it cannot itself drive a
subscription-covered model turn (see the note below). Instead the
``/t3:running-evals`` skill dispatches an in-session ``Agent`` sub-agent per
scenario; Claude Code writes that sub-agent's trajectory to
``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl``, and
``t3 eval capture-subagent`` copies it to the path this backend reads. The
backend auto-detects the transcript shape and grades it through the SAME
extractors the SDK path feeds the grader — so grading is identical.

Why no fully-automatic local-subscription backend: subscription coverage is a
property of an interactive Claude Code session driving an ``Agent`` sub-agent.
The eval CLI is a plain Python process; for it to spend subscription tokens it
would have to BE an in-session sub-agent. The captured sub-agent transcript is
the clean seam — the in-session ``/t3:running-evals`` driver produces it on the
subscription, the harness grades it offline. Both capture and grade read
on-disk files only, so the subscription lane never meters.
"""

from pathlib import Path
from typing import Protocol

from teatree.eval.auth import ensure_oauth_token
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.sdk_runner import MAX_BUDGET_USD, SdkInProcessRunner
from teatree.eval.subagent_transcript import is_subagent_transcript, subagent_run
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
    require_executed: bool = False,
    max_budget_usd: float = float(MAX_BUDGET_USD),
) -> EvalRunner:
    """Build the eval runner for *backend*.

    ``"sdk"`` → the metered in-process Agent-SDK runner. Resolves
    ``CLAUDE_CODE_OAUTH_TOKEN`` first (env wins for CI, else exports it from the
    ``pass`` store for local) so the runner's isolated-env copy and the docker
    pass-through both carry it without a manual ``export``.
    ``"subscription"`` → the transcript-ingest runner (local, subscription); it
    never authenticates a metered turn, so it does not resolve the token.

    ``require_executed`` only affects the sdk runner: it arms the hard-error on a
    missing ``claude`` binary so the all-skipped gate cannot be silently disarmed
    by an unprovisioned CLI. The subscription runner ignores it — its legitimate
    pre-transcript all-skip is caught downstream by :func:`guard_executed`.

    ``max_budget_usd`` is the sdk runner's per-run circuit breaker (default the
    cheap-lane :data:`~teatree.eval.sdk_runner.MAX_BUDGET_USD`); the subscription
    runner never meters, so it ignores it.
    """
    if backend == SDK_BACKEND:
        ensure_oauth_token()
        return SdkInProcessRunner(
            max_turns_override=max_turns_override,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
        )
    if backend == SUBSCRIPTION_BACKEND:
        return SubscriptionTranscriptRunner(transcript_dir=transcript_dir or Path.cwd())
    msg = f"unknown eval backend {backend!r}; expected one of {', '.join(KNOWN_BACKENDS)}"
    raise UnknownBackendError(msg)


class SubscriptionTranscriptRunner:
    """Grade a scenario from a subscription-produced transcript.

    Two transcript shapes are accepted, auto-detected per file:

    *   The ``claude -p --output-format stream-json`` shape, parsed by the same
        extractors the SDK backend feeds the grader.
    *   The in-session sub-agent JSONL Claude Code writes to
        ``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl`` — the
        ONLY transcript a subscription-covered turn produces, since spending
        subscription tokens requires an in-session ``Agent``. The
        ``/t3:running-evals`` skill dispatches one sub-agent per scenario and
        ``t3 eval capture-subagent`` copies its JSONL to the path
        :meth:`transcript_path` reports. The session schema shares the
        stream-json ``message.content[]`` block shape (so tool/text extraction is
        identical) and differs only at the terminus (no ``result`` event →
        completion via ``stop_reason``), handled by
        :mod:`teatree.eval.subagent_transcript`.

    Either way grading is identical to the SDK path, and neither path invokes
    ``claude -p`` — both read an on-disk transcript only, so the subscription
    lane never meters. A missing transcript yields a skip-shaped
    :class:`EvalRun` (terminal reason names the expected path) so a partial
    local run reports cleanly rather than erroring — symmetric with the SDK
    runner's missing-``claude`` skip.
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
        if is_subagent_transcript(raw):
            return subagent_run(spec, raw)
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
