"""CLI-free Claude eval execution over the Anthropic Messages API DIRECTLY.

The fourth :class:`~teatree.eval.backends.EvalRunner`. Where the ``api`` backend
runs a Claude model through ``claude-agent-sdk`` — which spawns the ``claude`` CLI
child, so ``shutil.which("claude")`` is its provisioning gate — this backend drives
the SAME Claude model through a ``pydantic_ai``
:class:`~pydantic_ai.models.anthropic.AnthropicModel` (the ``anthropic`` package →
``api.anthropic.com``). No ``claude`` binary is spawned, so a downstream harness
that forbids the Claude Code CLI can adopt teatree's ``--backend api`` eval lanes
([#3222](https://github.com/souliane/teatree/issues/3222)) while teatree's own
default lane keeps the CLI-backed SDK transport.

The ``claude_agent_sdk`` package still supplies the message *vocabulary* the grader
reads (``AssistantMessage`` / ``ResultMessage`` — the provider-agnostic intermediate
every backend yields), but no model call goes through it: the transport is the
Anthropic Messages API, not the CLI.

The agentic loop and the vocabulary mapping are SHARED with the ``pydantic_ai``
backend — this runner resolves an ``AnthropicModel`` and delegates the drive to
:class:`~teatree.eval.pydantic_ai_runner.PydanticAiRunner` (whose ``model`` is
injectable), so grading is byte-identical across the two fresh-run non-CLI lanes and
this module carries only the Anthropic-model resolution + the credential gate.

Credential: the metered :class:`~teatree.llm.credentials.AnthropicApiKeyCredential`
(``ANTHROPIC_API_KEY``, resolved lazily at run time — never at construction, so
building the runner needs no live key). Its absence is the provisioning gate,
symmetric with the ``api`` runner's missing-``claude`` skip: a missing key returns a
skip-shaped :class:`~teatree.eval.models.EvalRun`, unless ``require_executed`` arms
the all-skipped enforcement gate — then it raises
:class:`AnthropicApiKeyMissingError` on the FIRST scenario, the earliest fail-loud
point.
"""

import dataclasses

from claude_agent_sdk.types import EffortLevel
from pydantic_ai.models import Model

from teatree.config import get_effective_settings
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.model_variant import parse_model_variant
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.pydantic_ai_runner import PydanticAiRunner
from teatree.llm.credentials import AnthropicApiKeyCredential, Credential, CredentialError


class AnthropicApiKeyMissingError(RuntimeError):
    """Raised when ``ANTHROPIC_API_KEY`` is unresolvable while the all-skipped gate is armed.

    ``require_executed`` callers (the CLI-free metered eval job) cannot tolerate a
    decorative skip: with no key the Anthropic backend can execute nothing, so the
    suite would report an all-skipped green. Raising on the first scenario fails the
    job at the earliest point — before any scenario is graded.
    """


class AnthropicApiRunner:
    """Run an :class:`EvalSpec` against the Anthropic Messages API — the CLI-free Claude lane.

    *model* is INJECTABLE (default ``None`` resolves the real ``AnthropicModel``
    lazily inside :meth:`run`, so building the runner never needs a live key): a test
    drives it with pydantic_ai's own :class:`~pydantic_ai.models.test.TestModel` /
    :class:`~pydantic_ai.models.function.FunctionModel` doubles, no network, no token.
    *credential* is likewise injectable so a test exercises the missing-key skip and
    the ``require_executed`` fail-loud without touching the real environment.
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        turn_cap: int | None = None,
        effort: EffortLevel | None = None,
        require_executed: bool = False,
        credential: Credential | None = None,
    ) -> None:
        self._model = model
        #: The per-run request-loop cap for the delegated drive — an explicit
        #: ``--max-turns`` else the eval-lane guardrail, folded to one value by
        #: :func:`build_anthropic_api_eval_runner` (both bound the same loop).
        self._turn_cap = turn_cap
        #: Lane-level representative reasoning effort applied when a scenario declares
        #: no ``model@effort`` of its own (a declared effort wins).
        self._effort = effort
        self._require_executed = require_executed
        self._credential = credential or AnthropicApiKeyCredential()

    def run(self, spec: EvalSpec) -> EvalRun:
        # Resolve the abstract tier/phase to a concrete model id (a no-op when the
        # spec already carries a concrete ``model``); the resolved id names the
        # Anthropic API model and flows into the ledger label + report.
        spec = dataclasses.replace(spec, model=resolve_eval_model(spec))
        model = self._resolve_model_or_skip(spec)
        if model is None:
            return _skip_run(spec, "ANTHROPIC_API_KEY not resolvable")
        # Delegate the request loop + vocabulary mapping + watchdog to the pydantic_ai
        # lane, injecting the Anthropic model so its own model-resolution is
        # never reached; the turn cap bounds that loop.
        delegate = PydanticAiRunner(model=model, max_turns_override=self._turn_cap, effort=self._effort)
        return delegate.run(spec)

    def _resolve_model_or_skip(self, spec: EvalSpec) -> Model | None:
        """The injected model, else a real ``AnthropicModel``; ``None`` when the key is absent.

        An injected model (a test double) needs no key — the credential gate is only
        consulted for a real run. A missing key returns ``None`` (the caller skips)
        unless ``require_executed`` is armed, which raises the fail-loud error.
        """
        if self._model is not None:
            return self._model
        try:
            api_key = self._credential.resolve()
        except CredentialError as exc:
            if self._require_executed:
                msg = (
                    "ANTHROPIC_API_KEY not resolvable but --require-executed is armed: the CLI-free "
                    "anthropic_api backend can execute no scenario, so the suite would report an "
                    "all-skipped green. Set ANTHROPIC_API_KEY on the runner — anthropic_api + "
                    "require-executed must never decoratively skip."
                )
                raise AnthropicApiKeyMissingError(msg) from exc
            return None
        return _build_anthropic_model(spec, api_key)


def _build_anthropic_model(spec: EvalSpec, api_key: str) -> Model:
    """Build the ``pydantic_ai`` Anthropic model that talks to the Messages API directly.

    Imported at call time (not module top) so this module imports without the
    ``anthropic`` package present — a test that injects a model never triggers it,
    and the ``import`` chain stays light until a real Anthropic run is requested.
    """
    from pydantic_ai.models.anthropic import AnthropicModel  # noqa: PLC0415 — deferred lazy import
    from pydantic_ai.providers.anthropic import AnthropicProvider  # noqa: PLC0415 — deferred lazy import

    model_name = parse_model_variant(spec.model).model
    return AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))


def _skip_run(spec: EvalSpec, reason: str) -> EvalRun:
    """A skip-shaped run for an un-provisioned lane (no key), mirroring the ``api`` runner's skip."""
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=f"skipped: {reason}",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def build_anthropic_api_eval_runner(
    *,
    max_turns_override: int | None = None,
    effort: EffortLevel | None = None,
    require_executed: bool = False,
) -> AnthropicApiRunner:
    """Build the ``anthropic_api`` eval runner with the eval-lane request-loop guardrail.

    The DB-home per-run step cap is resolved SYNCHRONOUSLY here (never inside the
    delegated async drive, where a ``get_effective_settings`` read fails safe to
    defaults under Django's async guard), mirroring
    :func:`~teatree.eval.pydantic_ai_runner.build_pydantic_ai_eval_runner`. An
    explicit ``max_turns_override`` wins over the guardrail — both bound the same
    delegated request loop, so they fold to one ``turn_cap``.
    """
    settings = get_effective_settings()
    turn_cap = max_turns_override if max_turns_override is not None else settings.pydantic_ai_request_limit
    return AnthropicApiRunner(turn_cap=turn_cap, effort=effort, require_executed=require_executed)


__all__ = ["AnthropicApiKeyMissingError", "AnthropicApiRunner", "build_anthropic_api_eval_runner"]
