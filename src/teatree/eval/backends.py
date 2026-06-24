"""Pluggable execution backends for the behavioral eval harness.

The eval harness grades an :class:`~teatree.eval.models.EvalRun` regardless of
HOW the run was produced — the matchers only see captured tool calls and text
blocks. That makes the *execution* swappable.

Two backends, one ``EvalRunner`` protocol. The metered ``sdk`` backend bills the
metered ``ANTHROPIC_API_KEY`` — EXCLUSIVELY, never the subscription
``CLAUDE_CODE_OAUTH_TOKEN`` (a full run would throttle the subscription's usage
window and mislabel the throttled cells). The ``transcript`` backend runs no
model, so it authenticates nothing.

:class:`~teatree.eval.sdk_runner.SdkInProcessRunner` (``backend="sdk"``) RUNS the
model fresh in-process via ``claude-agent-sdk`` (the SDK spawns the ``claude``
CLI child). It spends metered API time; the per-invocation ``max_budget_usd``
circuit breaker bounds that spend. This is the automated path the CI eval job
uses.

:class:`TranscriptRunner` (``backend="transcript"``) REUSES an already-recorded
run — it grades an on-disk transcript that a previous subscription-covered turn
produced, so it costs ``$0`` extra (no model is run). A standalone ``t3 eval
run`` process has no in-session ``Agent`` tool, so it cannot itself drive a
subscription-covered model turn (see the note below). Instead the
``/t3:running-evals`` skill dispatches an in-session ``Agent`` sub-agent per
scenario; Claude Code writes that sub-agent's trajectory to
``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl``, and
``t3 eval capture-subagent`` copies it to the path this backend reads. The
backend auto-detects the transcript shape and grades it through the SAME
extractors the SDK path feeds the grader — so grading is identical.

Why no fully-automatic local backend reusing the subscription in-process:
spending subscription tokens from a plain Python process requires the process to
BE an in-session ``Agent`` sub-agent. The captured sub-agent transcript is the
clean seam — the in-session ``/t3:running-evals`` driver produces it, the harness
grades it offline. Both capture and grade read on-disk files only, so the
transcript lane runs no model.
"""

from pathlib import Path
from typing import Protocol

from claude_agent_sdk.types import EffortLevel

from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.sdk_runner import MAX_BUDGET_USD, SdkInProcessRunner
from teatree.eval.subagent_transcript import is_subagent_transcript, subagent_run
from teatree.eval.transcript import extract_terminal_reason, extract_text_blocks, extract_tool_calls, parse_stream_json
from teatree.llm.credentials import AnthropicApiKeyCredential

SDK_BACKEND = "sdk"
TRANSCRIPT_BACKEND = "transcript"
KNOWN_BACKENDS = (SDK_BACKEND, TRANSCRIPT_BACKEND)


class EvalRunner(Protocol):
    """Anything that turns an :class:`EvalSpec` into an :class:`EvalRun`."""

    def run(self, spec: EvalSpec) -> EvalRun: ...


class UnknownBackendError(ValueError):
    """Raised for a ``--backend`` value outside :data:`KNOWN_BACKENDS`."""


# ast-grep-ignore: ac-django-no-complexity-suppressions
def make_runner(  # noqa: PLR0913 — each kwarg threads one runner-construction knob (turns / budget / effort / require / transcript-dir) from the `t3 eval run` CLI; the list IS the backend contract.
    backend: str,
    *,
    max_turns_override: int | None = None,
    transcript_dir: Path | None = None,
    require_executed: bool = False,
    max_budget_usd: float = float(MAX_BUDGET_USD),
    effort: EffortLevel | None = None,
) -> EvalRunner:
    """Build the eval runner for *backend*.

    ``"sdk"`` → the in-process Agent-SDK runner that RUNS the model fresh, billed
    on the metered ``ANTHROPIC_API_KEY`` (never the subscription OAuth token).
    Resolves ``ANTHROPIC_API_KEY`` first (env wins for CI, else exports it from the
    ``pass`` store for local) via the canonical credential layer
    (:class:`~teatree.llm.credentials.AnthropicApiKeyCredential`) so the runner's
    isolated-env copy and the docker pass-through both carry it without a manual
    ``export``; a missing key fails loud with
    :class:`~teatree.llm.credentials.CredentialError` rather than throttling the
    subscription.
    ``"transcript"`` → the transcript-ingest runner that REUSES an
    already-recorded run; it runs no model, so it resolves no credential.

    ``require_executed`` only affects the sdk runner: it arms the hard-error on a
    missing ``claude`` binary so the all-skipped gate cannot be silently disarmed
    by an unprovisioned CLI. The transcript runner ignores it — its legitimate
    pre-transcript all-skip is caught downstream by :func:`guard_executed`.

    ``max_budget_usd`` is the sdk runner's per-run circuit breaker (default the
    cheap-lane :data:`~teatree.eval.sdk_runner.MAX_BUDGET_USD`); the transcript
    runner runs no model, so it ignores it.

    ``effort`` is the lane-level representative reasoning effort applied to a
    scenario that declares no ``model@effort`` of its own (the fresh-run lane runs
    at a representative effort, not the model's default); the transcript runner
    ignores it.
    """
    if backend == SDK_BACKEND:
        # Fail loud (CredentialError) before building the metered runner if no
        # ANTHROPIC_API_KEY is resolvable, and export it so the isolated child env
        # and docker pass-through carry it — the metered lane never falls back to
        # the subscription. isolated_claude_env then strips the conflicting OAuth
        # token from the child env using the same credential's spec.
        AnthropicApiKeyCredential().export()
        return SdkInProcessRunner(
            max_turns_override=max_turns_override,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
            effort=effort,
        )
    if backend == TRANSCRIPT_BACKEND:
        return TranscriptRunner(transcript_dir=transcript_dir or Path.cwd())
    msg = f"unknown eval backend {backend!r}; expected one of {', '.join(KNOWN_BACKENDS)}"
    raise UnknownBackendError(msg)


class TranscriptRunner:
    """Grade a scenario by REUSING an already-recorded subscription transcript.

    Runs no model — it reads an on-disk transcript a previous subscription-covered
    turn produced, so it costs ``$0`` extra. Two transcript shapes are accepted,
    auto-detected per file:

    *   The ``claude -p --output-format stream-json`` shape, parsed by the same
        extractors the SDK backend feeds the grader.
    *   The in-session sub-agent JSONL Claude Code writes to
        ``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl`` — the
        transcript a subscription-covered turn produces in-session, since spending
        subscription tokens requires an in-session ``Agent``. The
        ``/t3:running-evals`` skill dispatches one sub-agent per scenario and
        ``t3 eval capture-subagent`` copies its JSONL to the path
        :meth:`transcript_path` reports. The session schema shares the
        stream-json ``message.content[]`` block shape (so tool/text extraction is
        identical) and differs only at the terminus (no ``result`` event →
        completion via ``stop_reason``), handled by
        :mod:`teatree.eval.subagent_transcript`.

    Either way grading is identical to the SDK path, and neither path runs a
    model — both read an on-disk transcript only. A missing transcript yields a
    skip-shaped :class:`EvalRun` (terminal reason names the expected path) so a
    partial local run reports cleanly rather than erroring — symmetric with the
    SDK runner's missing-``claude`` skip.
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
                terminal_reason=f"skipped: no transcript at {path}",
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
