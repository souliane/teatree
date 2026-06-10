"""In-process ``claude-agent-sdk`` runner for behavioral evals.

Drives :func:`claude_agent_sdk.query` once per scenario inside an isolated,
clean-room configuration that reproduces what the deleted ``claude -p`` runner
achieved with ``--bare`` / ``isolated_claude_env`` and a wall of explicit flags:

*   ``setting_sources=[]`` — no ``user`` / ``project`` / ``local`` settings, so
    the developer's ``~/.claude``/project settings never bias a result;
*   ``system_prompt=<plain spec system prompt str>`` — the scenario's agent
    definition is the WHOLE system prompt (not the ``claude_code`` preset), so no
    built-in system prompt leaks in;
*   ``settings='{"hooks":{}}'`` and ``strict_mcp_config`` — no hooks, no MCP;
*   ``cwd=<isolated temp>`` + ``env`` from :func:`isolated_claude_env` — ``HOME``
    and the config-dir vars point at a ``.claude``-free temp directory so
    ``CLAUDE.md`` / auto-memory auto-discovery finds nothing;
*   ``permission_mode="bypassPermissions"`` and ``max_budget_usd`` — the budget
    circuit breaker the metered lane relies on.

The typed messages the SDK yields are mapped to the stream-json event dicts the
:mod:`teatree.eval.transcript` extractors already parse, so tool-call / text /
terminal / cost extraction is the SAME single path the subscription transcript
runner feeds the grader — and the produced :class:`EvalRun` is byte-identical in
shape to the old runner's, leaving report.py untouched.

``claude`` absence is still the skip / require-executed precondition: the SDK
spawns the ``claude`` CLI child, so ``shutil.which("claude")`` is the same
provisioning gate the ``claude -p`` runner used. When it is missing the runner
returns a skip-shaped :class:`EvalRun` (clean ``SKIP`` for un-provisioned
contributors), unless ``require_executed`` arms the all-skipped enforcement
gate — then a missing binary raises :class:`ClaudeCliMissingError` on the FIRST
scenario, the earliest fail-loud point.

The async ``query`` is bridged to the sync :meth:`SdkInProcessRunner.run` via
:func:`asyncio.run`, with a per-scenario wall-clock watchdog (:func:`asyncio.wait_for`).
"""

import asyncio
import dataclasses
import re
import shutil
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ContentBlock,
    Message,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)
from claude_agent_sdk.types import EffortLevel

from teatree.eval.context_budget import extract_sections
from teatree.eval.isolation import isolated_claude_env
from teatree.eval.model_variant import parse_model_variant
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.transcript import (
    extract_cost_usd,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    parse_stream_json,
)

WATCHDOG_SECONDS = 120
#: Per-run budget for the cheap lane (``t3 eval run``). Only the DEFAULT — the
#: metered benchmark threads a generous cap so a finishing scenario is measured,
#: not truncated. See :data:`CleanRoomConfig.max_budget_usd`.
MAX_BUDGET_USD = "0.10"
FALLBACK_MODEL = "claude-sonnet-4-6"
EMPTY_SETTINGS = '{"hooks":{}}'
BUDGET_EXCEEDED_REASON = "budget_exceeded"

#: The SDK has no typed budget exception: when ``max_budget_usd`` is hit the CLI
#: emits an ``error_max_budget_usd`` result and exits non-zero, which the SDK
#: surfaces as a bare ``Exception`` whose message contains "maximum budget"
#: (e.g. ``Claude Code returned an error result: Reached maximum budget ($0.1)``).
#: We match that substring defensively and re-raise everything else.
_BUDGET_EXCEEDED_MARKER = "maximum budget"
#: The cap the SDK reports in the message — ``Reached maximum budget ($0.1)`` —
#: is the partial-cost floor when no metered ``result`` event was produced.
_BUDGET_AMOUNT_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")

#: Typed alias callers may ``raise``/``except`` against. The SDK raises a bare
#: ``Exception`` for the budget breaker, so the runner ALSO matches the message
#: substring — this alias only types the direct-raise path, never narrows it.
BudgetExceededError = RuntimeError


def is_budget_exceeded_message(message: str) -> bool:
    """True when *message* is the SDK's budget-circuit-breaker error.

    The detection is a substring match on the SDK's wording because the SDK has
    no typed budget exception — see :data:`_BUDGET_EXCEEDED_MARKER`. Restricting
    to this exact marker keeps the runner's catch defensive: any other error
    message re-raises, so a genuine crash is never swallowed as a budget cell.
    """
    return _BUDGET_EXCEEDED_MARKER in message


def _budget_floor_from_message(message: str, *, cap: float) -> float:
    """Recover the partial cost from the SDK's ``Reached maximum budget ($X)``.

    Returns the amount the message names (the spend at truncation) when present,
    else the configured *cap* as a floor — an over-budget cell always reports a
    real cost, never a misleading ``0.0``/blank.
    """
    match = _BUDGET_AMOUNT_RE.search(message)
    return float(match.group(1)) if match else cap


class ClaudeCliMissingError(RuntimeError):
    """Raised when ``claude`` is not on PATH while the all-skipped gate is armed.

    ``require_executed`` callers (the metered CI eval job) cannot tolerate a
    decorative skip: a missing binary means the metered backend can execute
    nothing, so the suite would report an all-skipped green. Raising here fails
    the job at the earliest point — before any scenario is graded.
    """


@dataclasses.dataclass(frozen=True)
class CleanRoomConfig:
    """The inputs to a clean-room SDK invocation shared by runner and judge."""

    system_prompt: str
    workspace: Path
    cwd: str
    env: dict[str, str]
    allowed_tools: tuple[str, ...]
    model: str
    max_turns: int
    #: Reasoning-effort level (the SDK's first-class ``effort`` option, rendered
    #: as the ``claude --effort <level>`` flag). ``None`` = the model's default.
    effort: EffortLevel | None = None
    #: Per-run USD budget circuit breaker. Defaults to the cheap-lane
    #: :data:`MAX_BUDGET_USD`; the metered benchmark threads a generous cap so a
    #: high-effort scenario completes (a truncated run is a false measurement).
    max_budget_usd: float = float(MAX_BUDGET_USD)


def build_sdk_options(config: CleanRoomConfig) -> ClaudeAgentOptions:
    """Build the clean-room :class:`ClaudeAgentOptions` shared by runner and judge.

    Reproduces the deleted runner's virgin configuration: empty
    ``setting_sources`` (no personal context), a plain-string ``system_prompt``
    (the scenario's own definition, never the ``claude_code`` preset), empty
    hooks via ``settings``, ``strict_mcp_config``, ``bypassPermissions``, and the
    ``max_budget_usd`` circuit breaker. ``cwd``/``env`` come from
    :func:`isolated_claude_env`; ``add_dirs`` grants the scenario its workspace.
    """
    return ClaudeAgentOptions(
        setting_sources=[],
        system_prompt=config.system_prompt,
        settings=EMPTY_SETTINGS,
        strict_mcp_config=True,
        cwd=config.cwd,
        env=config.env,
        add_dirs=[str(config.workspace)],
        allowed_tools=list(config.allowed_tools),
        permission_mode="bypassPermissions",
        max_turns=config.max_turns,
        max_budget_usd=config.max_budget_usd,
        model=config.model,
        fallback_model=FALLBACK_MODEL,
        effort=config.effort,
    )


def load_agent_definition(agent_path: str, agent_sections: tuple[str, ...] = ()) -> str:
    """Read the agent definition (whole file, or only the named ``## `` sections)."""
    resolved = Path(agent_path).expanduser()
    if not resolved.is_absolute():
        for candidate in (Path.cwd() / resolved, _teatree_root() / resolved):
            if candidate.is_file():
                resolved = candidate
                break
    if not resolved.is_file():
        msg = f"Agent definition not found: {agent_path}"
        raise FileNotFoundError(msg)
    text = resolved.read_text(encoding="utf-8")
    if not text.strip():
        msg = f"Agent definition is empty: {resolved}"
        raise ValueError(msg)
    if agent_sections:
        return extract_sections(text, agent_sections)
    return text


class SdkInProcessRunner:
    """Run an :class:`EvalSpec` via the in-process Agent SDK and capture tool calls."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        max_turns_override: int | None = None,
        require_executed: bool = False,
        max_budget_usd: float = float(MAX_BUDGET_USD),
    ) -> None:
        self._workspace = workspace or Path.cwd()
        self._max_turns_override = max_turns_override
        self._require_executed = require_executed
        self._max_budget_usd = max_budget_usd

    def run(self, spec: EvalSpec) -> EvalRun:
        if shutil.which("claude") is None:
            if self._require_executed:
                msg = (
                    "claude binary not on PATH but --require-executed is armed: the metered "
                    "sdk backend can execute no scenario, so the suite would report an "
                    "all-skipped green. Provision the Claude CLI (and CLAUDE_CODE_OAUTH_TOKEN) "
                    "on the runner — sdk + require-executed must never decoratively skip."
                )
                raise ClaudeCliMissingError(msg)
            return self._skip_run(spec, "claude binary not on PATH")

        system_prompt = load_agent_definition(spec.agent_path, spec.agent_sections)
        max_turns = self._max_turns_override if self._max_turns_override is not None else spec.max_turns
        try:
            messages = asyncio.run(self._drive(spec, system_prompt=system_prompt, max_turns=max_turns))
        except TimeoutError:
            return self._terminal_run(spec, terminal_reason="timeout")
        except Exception as exc:
            if not is_budget_exceeded_message(str(exc)):
                raise
            return self._terminal_run(
                spec,
                terminal_reason=BUDGET_EXCEEDED_REASON,
                cost_usd=_budget_floor_from_message(str(exc), cap=self._max_budget_usd),
            )
        return _eval_run_from_messages(spec, messages)

    async def _drive(self, spec: EvalSpec, *, system_prompt: str, max_turns: int) -> list[Message]:
        variant = parse_model_variant(spec.model)
        with isolated_claude_env() as (env, cwd):
            options = build_sdk_options(
                CleanRoomConfig(
                    system_prompt=system_prompt,
                    workspace=self._workspace,
                    cwd=cwd,
                    env=env,
                    allowed_tools=spec.tools,
                    model=variant.model,
                    max_turns=max_turns,
                    effort=variant.effort,
                    max_budget_usd=self._max_budget_usd,
                )
            )
            return await asyncio.wait_for(_collect(spec.prompt, options), timeout=WATCHDOG_SECONDS)

    @staticmethod
    def _terminal_run(spec: EvalSpec, *, terminal_reason: str, cost_usd: float = 0.0) -> EvalRun:
        """Build an error-shaped :class:`EvalRun` for a run that never produced a transcript.

        The timeout and budget-exceeded paths share this shape: no captured tool
        calls/text, ``is_error=True``, and a terminal reason that grades the cell
        to a FAIL signal rather than crashing the run. Mirrors how the timeout
        path built its EvalRun before being extracted here.
        """
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=(),
            terminal_reason=terminal_reason,
            is_error=True,
            raw_stdout="",
            raw_stderr="",
            cost_usd=cost_usd,
        )

    @staticmethod
    def _skip_run(spec: EvalSpec, reason: str) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=(),
            terminal_reason=f"skipped: {reason}",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
        )


async def _collect(prompt: str, options: ClaudeAgentOptions) -> list[Message]:
    return [message async for message in query(prompt=prompt, options=options)]


def _eval_run_from_messages(spec: EvalSpec, messages: list[Message]) -> EvalRun:
    """Map the typed SDK messages onto the shared transcript extraction path.

    Each typed message is rendered to the stream-json event dict the
    :mod:`teatree.eval.transcript` extractors already parse, so tool/text/
    terminal/cost extraction is identical to the subscription transcript path.
    """
    raw_stdout = _synthesize_stream_json(messages)
    events = parse_stream_json(raw_stdout)
    terminal_reason, is_error = extract_terminal_reason(events)
    return EvalRun(
        spec_name=spec.name,
        tool_calls=tuple(extract_tool_calls(events)),
        text_blocks=tuple(extract_text_blocks(events)),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout=raw_stdout,
        raw_stderr="",
        cost_usd=extract_cost_usd(events),
    )


def _synthesize_stream_json(messages: list[Message]) -> str:
    import json  # noqa: PLC0415

    lines: list[str] = []
    for message in messages:
        event = _message_to_event(message)
        if event is not None:
            lines.append(json.dumps(event))
    return "\n".join(lines) + ("\n" if lines else "")


def _message_to_event(message: Message) -> dict[str, Any] | None:
    if isinstance(message, AssistantMessage):
        return {"type": "assistant", "message": {"content": [_block_to_dict(b) for b in message.content]}}
    if isinstance(message, ResultMessage):
        return {
            "type": "result",
            "subtype": message.subtype,
            "is_error": message.is_error,
            "total_cost_usd": message.total_cost_usd,
        }
    return None


def _block_to_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": dict(block.input)}
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    return {"type": "unknown"}


def _teatree_root() -> Path:
    """Return the teatree repo root (parent of ``src/teatree``)."""
    return Path(__file__).resolve().parents[3]
