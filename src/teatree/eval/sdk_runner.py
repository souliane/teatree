"""In-process ``claude-agent-sdk`` runner for behavioral evals.

Drives :func:`claude_agent_sdk.query` once per scenario inside an isolated,
clean-room configuration that reproduces what the deleted ``claude -p`` runner
achieved with ``--bare`` / ``isolated_claude_env`` and a wall of explicit flags:

*   ``setting_sources=[]`` — no ``user`` / ``project`` / ``local`` settings, so
    the developer's ``~/.claude``/project settings never bias a result;
*   ``system_prompt=<spilled to a --system-prompt-file>`` — the scenario's agent
    definition is the WHOLE system prompt (not the ``claude_code`` preset), so no
    built-in system prompt leaks in; it is passed by FILE, not argv, so a
    whole-skill prompt never blows ``ARG_MAX`` (E2BIG) at the spawn;
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
import os
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
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING
from teatree.eval.system_prompt_file import spill_system_prompt
from teatree.eval.toolset import compute_available_tools, compute_disallowed_tools
from teatree.eval.transcript import (
    extract_billed_model,
    extract_cost_usd,
    extract_model_cost_split,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    extract_usage,
    parse_stream_json,
    requested_model_present,
)
from teatree.eval.under_load import build_system_prompt, build_user_prompt

#: Env var names for the metered lane's GENEROUS, configurable resource caps. A
#: truncated run measures the cap, not behaviour (the first full metered run lost
#: ~18 scenarios to cap truncation — a false negative), so each default is
#: generous and overridable.
_WATCHDOG_ENV_VAR = "T3_EVAL_WATCHDOG_SECONDS"
_MAX_TURNS_ENV_VAR = "T3_EVAL_MAX_TURNS"
_METERED_BUDGET_ENV_VAR = "T3_EVAL_MAX_BUDGET_USD"
_METERED_EFFORT_ENV_VAR = "T3_EVAL_EFFORT"

#: GENEROUS per-scenario wall-clock watchdog (seconds). 120s was too tight for
#: sub-agent-spawning scenarios (an orchestrator that delegates an investigation
#: timed out before it finished), so the default is raised. Override via
#: ``T3_EVAL_WATCHDOG_SECONDS``.
DEFAULT_WATCHDOG_SECONDS = 300

#: GENEROUS default per-run budget for the metered ``t3 eval run --backend sdk``
#: lane — distinct from the cheap-lane :data:`MAX_BUDGET_USD` runner floor (0.10),
#: which truncated finishing scenarios (a truncated run measures the cap, not
#: behaviour). ~10x the cheap floor, below the benchmark's 2.0; override via
#: ``T3_EVAL_MAX_BUDGET_USD``.
METERED_DEFAULT_BUDGET_USD = 1.0

#: The metered lane's representative reasoning effort. The lane otherwise runs at
#: the model's DEFAULT effort, while real usage is high effort — so a default-effort
#: pass-rate is pessimistic. A scenario's own ``@effort`` still wins. Override via
#: ``T3_EVAL_EFFORT``.
METERED_DEFAULT_EFFORT: EffortLevel = "high"


def env_float(name: str, *, default: float) -> float:
    """Resolve a positive ``float`` from env *name*, falling back to *default*.

    A missing, empty, unparsable, or non-positive value yields the generous
    *default* — a fat-fingered override never silently tightens the cap to an
    accidental 0.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def resolve_watchdog_seconds() -> float:
    """The generous per-scenario watchdog, ``T3_EVAL_WATCHDOG_SECONDS`` overriding the default."""
    return env_float(_WATCHDOG_ENV_VAR, default=float(DEFAULT_WATCHDOG_SECONDS))


def resolve_max_turns_override(explicit: int | None = None) -> int | None:
    """An *explicit* override wins; else the ``T3_EVAL_MAX_TURNS`` knob; else ``None`` to defer to spec.

    Defers to each scenario's own ``max_turns`` (the per-scenario turn budget, mirroring
    per-scenario cost) when neither is set; a missing/empty/unparsable/non-positive env value
    yields ``None`` — never a silent global turn cap.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(_MAX_TURNS_ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def resolve_metered_budget_usd() -> float:
    """The generous metered-lane budget, ``T3_EVAL_MAX_BUDGET_USD`` overriding the default."""
    return env_float(_METERED_BUDGET_ENV_VAR, default=METERED_DEFAULT_BUDGET_USD)


def resolve_metered_effort() -> EffortLevel:
    """The representative metered-lane effort, ``T3_EVAL_EFFORT`` overriding the default.

    An invalid/unknown override falls back to the representative default rather
    than passing a bad level through to the SDK.
    """
    from teatree.eval.model_variant import EFFORT_LEVELS  # noqa: PLC0415 — avoid an import cycle at module load.

    raw = os.environ.get(_METERED_EFFORT_ENV_VAR, "").strip()
    return raw if raw in EFFORT_LEVELS else METERED_DEFAULT_EFFORT  # type: ignore[return-value]


#: Resolved at import so the existing ``patch("…WATCHDOG_SECONDS", 0.01)`` test seam
#: keeps working; the env override is read here once, generous by default.
WATCHDOG_SECONDS = resolve_watchdog_seconds()
#: Per-run budget for the cheap lane (``t3 eval run`` internal/runner default). Only
#: the DEFAULT — the metered ``t3 eval run --backend sdk`` lane and the benchmark
#: thread a generous cap so a finishing scenario is measured, not truncated. See
#: :data:`CleanRoomConfig.max_budget_usd` and ``METERED_DEFAULT_BUDGET_USD``.
MAX_BUDGET_USD = "0.10"
FALLBACK_MODEL = "claude-sonnet-4-6"
EMPTY_SETTINGS = '{"hooks":{}}'
BUDGET_EXCEEDED_REASON = "budget_exceeded"
MAX_TURNS_REASON = "max_turns"

#: The SDK has no typed error-result exception: when a run hits a cap the CLI
#: emits an ``is_error`` ``result`` event and exits non-zero, which the SDK's
#: ``receive_messages`` (``claude_agent_sdk/_internal/query.py`` L852) surfaces as
#: a bare ``Exception`` whose message is ``"Claude Code returned an error result:
#: <subtype-or-errors>"`` (built at L342). The trailing text is the CLI's own
#: error string, so each terminal subtype is identified by a stable substring:
#:
#: * ``error_max_budget_usd`` -> ``"Reached maximum budget ($0.1)"``
#: * ``error_max_turns``      -> ``"Reached maximum number of turns (3)"``
#:
#: A capped run is a GRADED terminus (the agent ran out of room), not an infra
#: failure — so each marker maps to a ``terminal_reason``. Anything NOT matched
#: here is a genuine error and re-raises, so a real crash is never swallowed as a
#: graded cell. Extend by adding a ``(marker, reason)`` pair.
_TERMINAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("maximum budget", BUDGET_EXCEEDED_REASON),
    ("maximum number of turns", MAX_TURNS_REASON),
)
#: The SDK wraps the CLI's non-zero exit as ``"Claude Code returned an error
#: result: <subtype-or-errors>"`` (``claude_agent_sdk/_internal/query.py`` L342)
#: whenever a ``result`` event carried ``is_error=True`` — but the descriptive
#: field it falls back to is the ``subtype``, which the CLI sometimes reports as
#: ``"success"`` even while exiting non-zero. That is a SUCCESSFUL terminus
#: mislabeled, NOT a cap and NOT a crash: the captured trajectory already holds
#: the success ``result`` event, so the run is graded normally instead of raised.
_SUCCESS_RESULT_MARKER = "returned an error result: success"
#: The cap the SDK reports in the budget message — ``Reached maximum budget
#: ($0.1)`` — is the partial-cost floor when no metered ``result`` event was
#: produced. (max-turns carries no ``($X)``; its cost comes from a captured
#: ``ResultMessage`` if any, else ``0.0``.)
_BUDGET_AMOUNT_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")

#: Typed alias callers may ``raise``/``except`` against. The SDK raises a bare
#: ``Exception`` for the budget breaker, so the runner ALSO matches the message
#: substring — this alias only types the direct-raise path, never narrows it.
BudgetExceededError = RuntimeError


def classify_terminal_error(message: str) -> str | None:
    """Map an SDK error-result *message* to a graded ``terminal_reason``, or ``None``.

    Returns the ``terminal_reason`` for a known terminal cap (budget, max-turns —
    see :data:`_TERMINAL_MARKERS`) when the message carries that cap's marker
    substring, else ``None`` for a genuine error the runner must re-raise. The
    markers are the CLI's own error-result strings (see :data:`_TERMINAL_MARKERS`
    for provenance); the list is the single place to extend with a new cap.
    """
    for marker, reason in _TERMINAL_MARKERS:
        if marker in message:
            return reason
    return None


def is_success_result_error(message: str) -> bool:
    """``True`` when the SDK's error-result *message* actually describes a SUCCESS.

    The CLI can exit non-zero while its ``result`` event subtype reads
    ``"success"``; the SDK then raises ``"...returned an error result: success"``.
    Treating that as a genuine error would crash a finished run, so the runner
    recognizes it and grades the captured trajectory normally (the success
    ``result`` event is already in the captured messages).
    """
    return _SUCCESS_RESULT_MARKER in message


def _budget_amount_from_message(message: str) -> float | None:
    """Return the ``$X`` amount the SDK message names, or ``None`` when absent.

    The budget cap message carries the spend at truncation (``Reached maximum
    budget ($0.1)``); the max-turns message carries none. ``None`` lets the caller
    pick a per-terminus fallback (the cap for budget, a captured ``ResultMessage``
    cost or ``0.0`` for max-turns).
    """
    match = _BUDGET_AMOUNT_RE.search(message)
    return float(match.group(1)) if match else None


def _budget_floor_from_message(message: str, *, cap: float) -> float:
    """Recover the partial cost from the SDK's ``Reached maximum budget ($X)``.

    Returns the amount the message names (the spend at truncation) when present,
    else the configured *cap* as a floor — an over-budget cell always reports a
    real cost, never a misleading ``0.0``/blank.
    """
    amount = _budget_amount_from_message(message)
    return amount if amount is not None else cap


class _TerminalResultError(Exception):
    """A known terminal cap (budget/max-turns) the SDK surfaced mid-stream.

    Carries the partial trajectory ``_collect`` gathered before the cap plus the
    classified ``terminal_reason``, so the runner can grade the REAL trajectory
    instead of discarding every message the all-or-nothing comprehension held.
    """

    def __init__(self, *, terminal_reason: str, messages: list[Message], cause: Exception) -> None:
        super().__init__(str(cause))
        self.terminal_reason = terminal_reason
        self.messages = messages
        self.cause = cause


class _SuccessMislabelResultError(Exception):
    """A finished SUCCESS the CLI mislabeled by exiting non-zero on a ``"success"`` subtype.

    The captured ``result`` event already reads ``subtype="success"`` but also
    carries a stray ``is_error=True`` (the CLI exited non-zero), so grading the
    trajectory as-is would force a finished, all-matchers-pass run to FAIL on the
    flag alone. Carries the captured trajectory so the runner can grade the REAL
    messages and clear ``is_error`` — the same correction
    :meth:`SdkInProcessRunner._terminal_capped_run` applies to a capped run.
    """

    def __init__(self, *, messages: list[Message], cause: Exception) -> None:
        super().__init__(str(cause))
        self.messages = messages
        self.cause = cause


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
    #: Tools to REMOVE from the model's available set (the SDK's
    #: ``--disallowedTools`` lever). Unlike ``allowed_tools`` it restricts the
    #: toolset even under ``bypassPermissions``. Defaults empty so the judge path
    #: (which shares this config) is unchanged; the runner computes the scenario's
    #: complement via :func:`compute_disallowed_tools`.
    disallowed_tools: tuple[str, ...] = ()
    #: The ``--tools`` ALLOWLIST (the SDK's ``ClaudeAgentOptions.tools``) — the
    #: model sees ONLY these, the PRIMARY restriction. Defaults empty so the judge
    #: path gets ``tools=None`` (the CLI default toolset); the runner computes the
    #: scenario's set via :func:`compute_available_tools`. An empty value renders
    #: as ``tools=None`` (CLI default), NEVER an empty ``--tools ""`` (no tools).
    available_tools: tuple[str, ...] = ()


def build_sdk_options(config: CleanRoomConfig) -> ClaudeAgentOptions:
    """Build the clean-room :class:`ClaudeAgentOptions` shared by runner and judge.

    Reproduces the deleted runner's virgin configuration: empty
    ``setting_sources`` (no personal context), the scenario's own definition as
    the WHOLE system prompt (never the ``claude_code`` preset), empty hooks via
    ``settings``, ``strict_mcp_config``, ``bypassPermissions``, and the
    ``max_budget_usd`` circuit breaker. ``cwd``/``env`` come from
    :func:`isolated_claude_env`; ``add_dirs`` grants the scenario its workspace.

    The system prompt is spilled to a file under ``cwd`` and passed as a
    :class:`SystemPromptFile` (``--system-prompt-file``), NOT as a plain string
    (``--system-prompt <text>``): a whole-skill system prompt sent via argv blows
    ``ARG_MAX`` (E2BIG) at spawn, failing the run before any scenario executes.
    The file path keeps the argv bounded regardless of skill size.

    The ``--tools`` allowlist is the PRIMARY toolset restriction: an empty
    ``available_tools`` is passed as ``tools=None`` (the CLI default toolset), NOT
    an empty list — the SDK renders ``[]`` as ``--tools ""`` (no tools), which
    would silently strip every tool from a scenario that did not opt into an
    allowlist.
    """
    available = list(config.available_tools) if config.available_tools else None
    return ClaudeAgentOptions(
        setting_sources=[],
        system_prompt=spill_system_prompt(config.system_prompt, config.cwd),
        settings=EMPTY_SETTINGS,
        strict_mcp_config=True,
        cwd=config.cwd,
        env=config.env,
        add_dirs=[str(config.workspace)],
        tools=available,
        allowed_tools=list(config.allowed_tools),
        disallowed_tools=list(config.disallowed_tools),
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
        effort: EffortLevel | None = None,
    ) -> None:
        self._workspace = workspace or Path.cwd()
        self._max_turns_override = max_turns_override
        self._require_executed = require_executed
        self._max_budget_usd = max_budget_usd
        #: Lane-level representative reasoning effort. Applied when a scenario
        #: declares no ``model@effort`` of its own (a declared effort wins).
        self._effort = effort

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

        clean_room_prompt = load_agent_definition(spec.agent_path, spec.agent_sections) + LIVE_ENV_FRAMING
        system_prompt = build_system_prompt(spec, clean_room_prompt=clean_room_prompt)
        max_turns = self._max_turns_override if self._max_turns_override is not None else spec.max_turns
        try:
            messages = asyncio.run(self._drive(spec, system_prompt=system_prompt, max_turns=max_turns))
        except TimeoutError:
            return self._terminal_run(spec, terminal_reason="timeout")
        except _TerminalResultError as terminal:
            return self._terminal_capped_run(spec, terminal)
        except _SuccessMislabelResultError as mislabel:
            return self._success_mislabel_run(spec, mislabel)
        return _eval_run_from_messages(spec, messages)

    def _terminal_capped_run(self, spec: EvalSpec, terminal: _TerminalResultError) -> EvalRun:
        """Grade a run the SDK terminated at a known cap (budget/max-turns).

        When the agent produced a trajectory before the cap, grade the REAL
        trajectory: build via :func:`_eval_run_from_messages` so the matchers
        decide pass/fail on what the agent actually did, then stamp the classified
        ``terminal_reason`` (so the renderer shows ``max_turns``/``budget_exceeded``)
        and clear ``is_error`` — a capped run that satisfied its matchers must not
        be forced to FAIL; the cap is surfaced via ``terminal_reason``, not by
        marking the run errored. Recover cost from the message's ``($X)`` (budget),
        else from any captured ``ResultMessage`` cost; when neither names a cost (a
        max-turns run with no metered result) cost is ``0.0``, with the
        ``terminal_reason`` making the incompleteness visible. Only when NOTHING
        was captured does it fall back to the empty :meth:`_terminal_run` shape —
        whose budget cost floors to the cap, the existing over-budget behavior.
        """
        message_amount = _budget_amount_from_message(str(terminal.cause))
        if not terminal.messages:
            # Floor the over-budget cost to the scenario's EFFECTIVE budget (its own
            # per-scenario override when set, else the run default) so a raised-cap
            # scenario reports the cap it actually ran under, not the shared default.
            effective_cap = spec.max_budget_usd if spec.max_budget_usd is not None else self._max_budget_usd
            cost = _budget_floor_from_message(str(terminal.cause), cap=effective_cap)
            empty_cost = cost if terminal.terminal_reason == BUDGET_EXCEEDED_REASON else 0.0
            return self._terminal_run(spec, terminal_reason=terminal.terminal_reason, cost_usd=empty_cost)
        graded = _eval_run_from_messages(spec, terminal.messages)
        cost = message_amount if message_amount is not None else graded.cost_usd
        return dataclasses.replace(
            graded,
            terminal_reason=terminal.terminal_reason,
            is_error=False,
            cost_usd=cost,
        )

    @staticmethod
    def _success_mislabel_run(spec: EvalSpec, mislabel: _SuccessMislabelResultError) -> EvalRun:
        """Grade a finished SUCCESS the CLI mislabeled by exiting non-zero.

        The captured ``result`` event reads ``subtype="success"`` but carries a
        stray ``is_error=True`` (the CLI exited non-zero on the success subtype).
        Grade the REAL trajectory via :func:`_eval_run_from_messages` so the
        matchers decide pass/fail, then clear ``is_error`` — exactly the correction
        :meth:`_terminal_capped_run` applies — so a finished, all-matchers-pass run
        is not forced to FAIL on the flag alone (:attr:`ScenarioResult.passed` fails
        on ``is_error`` BEFORE consulting matchers). The ``terminal_reason`` already
        reads ``success`` and is left untouched — this is a finished run, not a cap.
        """
        graded = _eval_run_from_messages(spec, mislabel.messages)
        return dataclasses.replace(graded, is_error=False)

    async def _drive(self, spec: EvalSpec, *, system_prompt: str, max_turns: int) -> list[Message]:
        variant = parse_model_variant(spec.model)
        # A scenario's own ``model@effort`` is authoritative; the lane-level
        # representative effort applies only when the scenario declares none.
        effort = variant.effort if variant.effort is not None else self._effort
        # A scenario's own per-scenario cost/time cap is authoritative; a
        # delegation scenario raises these to FIT a legitimate sub-agent TDD cycle
        # (worktree provision + red/green + commit) that costs/takes more than the
        # shared default — so a #2192 cap-tainted trial does not red the scenario for
        # a reason unrelated to behaviour. The run-level values apply when a scenario
        # declares none. The matchers are unchanged; this caps the RUN, not the teeth.
        max_budget_usd = spec.max_budget_usd if spec.max_budget_usd is not None else self._max_budget_usd
        watchdog = spec.watchdog_seconds if spec.watchdog_seconds is not None else WATCHDOG_SECONDS
        with isolated_claude_env() as (env, cwd):
            options = build_sdk_options(
                CleanRoomConfig(
                    system_prompt=system_prompt,
                    workspace=self._workspace,
                    cwd=cwd,
                    env=env,
                    allowed_tools=spec.tools,
                    available_tools=compute_available_tools(spec),
                    disallowed_tools=compute_disallowed_tools(spec),
                    model=variant.model,
                    max_turns=max_turns,
                    effort=effort,
                    max_budget_usd=max_budget_usd,
                )
            )
            return await asyncio.wait_for(_collect(build_user_prompt(spec), options), timeout=watchdog)

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
    """Stream the query, accumulating messages so a terminal cap keeps the partial run.

    The SDK raises a bare ``Exception`` mid-stream for any error-result subtype
    (``claude_agent_sdk/_internal/query.py`` L852) AFTER the messages emitted
    before the cap have already reached this loop. Accumulating into ``messages``
    as they arrive — instead of an all-or-nothing comprehension — keeps every
    ``AssistantMessage`` (with its tool calls) the agent produced before hitting
    the cap. On a KNOWN terminal cap the partial list is re-raised inside a
    :class:`_TerminalResultError` for the runner to grade; a SUCCESS mislabeled as
    an error result (the CLI exited non-zero on a ``"success"`` subtype) is
    re-raised inside a :class:`_SuccessMislabelResultError` so the runner grades the
    captured messages AND clears the stray ``is_error``; any other error re-raises
    unchanged so a genuine crash is never swallowed.
    """
    messages: list[Message] = []
    try:
        async for message in query(prompt=prompt, options=options):
            # NOT a comprehension: the suggested `extend([... async for ...])` is
            # all-or-nothing — it would discard the partial trajectory on the
            # mid-stream terminal raise, which is the exact bug this fixes.
            messages.append(message)  # noqa: PERF401 — partial list must survive a mid-iteration Exception
    except Exception as exc:
        if is_success_result_error(str(exc)):
            raise _SuccessMislabelResultError(messages=messages, cause=exc) from exc
        reason = classify_terminal_error(str(exc))
        if reason is None:
            raise
        raise _TerminalResultError(terminal_reason=reason, messages=messages, cause=exc) from exc
    return messages


def _eval_run_from_messages(spec: EvalSpec, messages: list[Message]) -> EvalRun:
    """Map the typed SDK messages onto the shared transcript extraction path.

    Each typed message is rendered to the stream-json event dict the
    :mod:`teatree.eval.transcript` extractors already parse, so tool/text/
    terminal/cost extraction is identical to the subscription transcript path.
    """
    raw_stdout = _synthesize_stream_json(messages)
    events = parse_stream_json(raw_stdout)
    terminal_reason, is_error = extract_terminal_reason(events)
    present = requested_model_present(events, spec.model)
    split = extract_model_cost_split(events, spec.model)
    return EvalRun(
        spec_name=spec.name,
        tool_calls=tuple(extract_tool_calls(events)),
        text_blocks=tuple(extract_text_blocks(events)),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout=raw_stdout,
        raw_stderr="",
        cost_usd=extract_cost_usd(events),
        usage=extract_usage(events),
        billed_model=extract_billed_model(events),
        fell_back=None if present is None else not present,
        main_cost_usd=split.main_cost_usd,
        aux_cost_usd=split.aux_cost_usd,
        main_usage=split.main_usage,
        aux_usage=split.aux_usage,
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
        # ``parent_tool_use_id`` distinguishes a TOP-LEVEL (main-agent) turn —
        # ``None`` per the SDK contract — from a sub-agent SIDECHAIN turn, which
        # carries the parent ``Agent``/``Task`` tool_use id. Threading it through
        # to the synthesized event lets :func:`extract_tool_calls` count only the
        # main agent's own calls; a sub-agent's worktree ``.py`` edits, emitted
        # inline into the same ``query`` stream, must NOT be attributed to the main
        # agent (the #2596 mis-attribution that failed delegates/full_speed RED).
        return {
            "type": "assistant",
            "message": {"content": [_block_to_dict(b) for b in message.content]},
            "parent_tool_use_id": message.parent_tool_use_id,
        }
    if isinstance(message, ResultMessage):
        return {
            "type": "result",
            "subtype": message.subtype,
            "is_error": message.is_error,
            "total_cost_usd": message.total_cost_usd,
            "usage": message.usage,
            "model_usage": message.model_usage,
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
