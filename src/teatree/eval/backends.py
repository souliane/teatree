"""Pluggable execution backends for the behavioral eval harness.

The eval harness grades an :class:`~teatree.eval.models.EvalRun` regardless of
HOW the run was produced — the matchers only see captured tool calls and text
blocks. That makes the *execution* swappable.

Two backends, one ``EvalRunner`` protocol. The fresh-run ``api`` backend
authenticates with the credential the ``eval_credential`` knob selects — the
subscription ``CLAUDE_CODE_OAUTH_TOKEN`` by DEFAULT (reversing
[#2707](https://github.com/souliane/teatree/issues/2707)) or, when the knob is set
to ``metered_api_key``, the metered ``ANTHROPIC_API_KEY``. The credential kind is
resolved through the single seam ``teatree.credential_config.resolve_eval_credential``
so flipping the knob switches every eval chokepoint at once (no per-call-site edit).
The ``transcript`` backend runs no model, so it authenticates nothing.

The default subscription lane draws no per-token bill but rides the plan's
depleting 5h/7d usage window — so the CI eval lane MUST be right-sized (a single
effort tier, a smaller trial count, per-account routing via
``anthropic_oauth_pass_paths``) or a full fan-out throttles the window mid-run AND
starves the main loop (same token). The metered lane has no such window (per-token
cost instead) and stays selectable via the knob.

:class:`~teatree.eval.api_runner.ApiInProcessRunner` (``backend="api"``) RUNS the
model fresh in-process via ``claude-agent-sdk`` (the SDK spawns the ``claude``
CLI child). The per-invocation ``max_budget_usd`` circuit breaker bounds the spend
(a hard cost cap on the metered lane; a usage-window guard on the subscription
lane). This is the automated path the CI eval job uses.

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

import dataclasses
from pathlib import Path
from typing import Protocol

from claude_agent_sdk.types import EffortLevel

from teatree.eval.api_runner import MAX_BUDGET_USD, ApiInProcessRunner
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.subagent_transcript import is_subagent_transcript, subagent_run
from teatree.eval.transcript import extract_terminal_reason, extract_text_blocks, extract_tool_calls, parse_stream_json

API_BACKEND = "api"
TRANSCRIPT_BACKEND = "transcript"
KNOWN_BACKENDS = (API_BACKEND, TRANSCRIPT_BACKEND)


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

    ``"api"`` → the in-process Agent-SDK runner that RUNS the model fresh, on the
    credential the ``eval_credential`` knob selects (default subscription OAuth,
    reversing #2707; ``metered_api_key`` for the metered key). Resolves it through
    ``resolve_eval_credential`` (env wins for CI, else exports it from the ``pass``
    store for local) so the runner's isolated-env copy and the docker pass-through
    both carry it without a manual ``export``, and hands the runner the credential's
    ``spec.conflicting_vars`` so the isolated child strips the OTHER credential; a
    missing credential fails loud with
    :class:`~teatree.llm.credentials.CredentialError` rather than authenticating as
    nothing.
    ``"transcript"`` → the transcript-ingest runner that REUSES an
    already-recorded run; it runs no model, so it resolves no credential.

    ``require_executed`` only affects the api runner: it arms the hard-error on a
    missing ``claude`` binary so the all-skipped gate cannot be silently disarmed
    by an unprovisioned CLI. The transcript runner ignores it — its legitimate
    pre-transcript all-skip is caught downstream by :func:`guard_executed`.

    ``max_budget_usd`` is the api runner's per-run circuit breaker (default the
    cheap-lane :data:`~teatree.eval.api_runner.MAX_BUDGET_USD`); the transcript
    runner runs no model, so it ignores it.

    ``effort`` is the lane-level representative reasoning effort applied to a
    scenario that declares no ``model@effort`` of its own (the fresh-run lane runs
    at a representative effort, not the model's default); the transcript runner
    ignores it.
    """
    if backend == API_BACKEND:
        # Resolve the SELECTED eval credential (the ``eval_credential`` knob — default
        # subscription OAuth, reversing #2707) and export it, so the isolated child
        # env and the docker pass-through carry it; a missing credential fails loud
        # with CredentialError before the runner exists. The runner is then handed the
        # credential's ``spec.conflicting_vars`` so ``isolated_claude_env`` strips the
        # OTHER credential (the OAuth lane strips the API key; the metered lane strips
        # the OAuth token) — "use THIS eval credential, exclusively". Imported at call
        # time (not module top) to keep the eval CLI import chain Django-free —
        # ``credential_config`` pulls in the routing models + settings, which cannot be
        # created before ``django.setup()`` (the plain ``import teatree.cli`` path).
        from teatree.credential_config import resolve_eval_credential  # noqa: PLC0415

        credential = resolve_eval_credential()
        credential.export()
        return ApiInProcessRunner(
            max_turns_override=max_turns_override,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
            effort=effort,
            conflicting_vars=credential.spec.conflicting_vars,
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
        # Resolve the abstract tier/phase to a concrete model id so the ledger
        # label + report read a real model, identical to the SDK runner. No model
        # runs here (the transcript is already recorded), so this is label-only.
        spec = dataclasses.replace(spec, model=resolve_eval_model(spec))
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
