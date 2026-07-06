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

The async ``query`` is bridged to the sync :meth:`ApiInProcessRunner.run` via
:func:`asyncio.run`, with a per-scenario wall-clock watchdog (:func:`asyncio.wait_for`).
"""

import asyncio
import dataclasses
import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, Message, query
from claude_agent_sdk.types import EffortLevel, SdkPluginConfig

from teatree.eval.api_errors import (
    BUDGET_EXCEEDED_REASON,
    SuccessMislabelResultError,
    TerminalResultError,
    budget_amount_from_message,
    budget_floor_from_message,
    classify_terminal_error,
    is_success_result_error,
)
from teatree.eval.context_budget import extract_sections
from teatree.eval.ephemeral_checkout import ephemeral_checkout_env, provision_ephemeral_checkout
from teatree.eval.git_fixture import provision_git_fixture
from teatree.eval.isolation import isolated_claude_env
from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.model_variant import parse_model_variant
from teatree.eval.models import CLEAN_ROOM_LANE, CLEAN_ROOM_MIN_TURNS, EvalRun, EvalSpec
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING
from teatree.eval.system_prompt_file import spill_system_prompt
from teatree.eval.toolset import (
    build_delegation_agents,
    compute_available_tools,
    compute_disallowed_tools,
    scenario_exposes_subagent_spawn,
)
from teatree.eval.under_load import build_system_prompt, build_user_prompt
from teatree.llm.anthropic_limits import CreditExhaustedError, LimitCause, classify_limit
from teatree.llm.credentials import AnthropicApiKeyCredential

#: The runner's default credential-conflict strip set — the metered lane's conflicts
#: (strip the subscription OAuth token), read off the credential class (its single
#: source of truth). ``make_runner`` overrides it with the SELECTED eval credential's
#: ``spec.conflicting_vars``; a direct construction keeps the pre-#2707-reversal
#: metered strip so isolation-only tests are unchanged.
_DEFAULT_CONFLICTING_VARS = AnthropicApiKeyCredential().spec.conflicting_vars

#: Env var names for the metered lane's GENEROUS, configurable resource caps. A
#: truncated run measures the cap, not behaviour (the first full metered run lost
#: ~18 scenarios to cap truncation — a false negative), so each default is
#: generous and overridable.
_WATCHDOG_ENV_VAR = "T3_EVAL_WATCHDOG_SECONDS"
_MAX_TURNS_ENV_VAR = "T3_EVAL_MAX_TURNS"
_METERED_BUDGET_ENV_VAR = "T3_EVAL_MAX_BUDGET_USD"
_METERED_EFFORT_ENV_VAR = "T3_EVAL_EFFORT"

#: ``shutil.which("claude")`` transiently returns ``None`` when the bundled Claude
#: Code CLI auto-updates MID-RUN — the nvm symlink is swapped out for a moment, so
#: the binary momentarily resolves to nothing. A hard fail there fires
#: ``ClaudeCliMissingError`` and reds every REMAINING scenario in the batch (one
#: observed run lost 191/197 scenarios to a single transient miss). The resolver
#: re-probes with a short bounded backoff so a mid-run swap is ridden out, while a
#: genuinely-absent binary still fails after the bounded attempts are exhausted.
CLAUDE_RESOLVE_MAX_ATTEMPTS = 4
CLAUDE_RESOLVE_BACKOFF_SECONDS = 0.5

#: GENEROUS per-scenario wall-clock watchdog (seconds). 120s was too tight for
#: sub-agent-spawning scenarios (an orchestrator that delegates an investigation
#: timed out before it finished), so the default is raised. Override via
#: ``T3_EVAL_WATCHDOG_SECONDS``.
#:
#: In the EVAL lane, COST ($) and TURNS are the meaningful gates — a
#: behaviourally-correct trajectory bounded by its ``max_budget_usd`` /
#: ``max_turns`` must NOT be falsely red'd by latency alone (#2192: a
#: ``timeout`` cap-taints the pass@k aggregate exactly like a budget/turn cap,
#: so a slow-but-correct trial reds a scenario whose other trial passed). The
#: wall-clock watchdog is therefore only a GENEROUS hang-backstop: high enough
#: that a slow-but-correct fan-out/delegation trajectory finishes inside it,
#: yet FINITE so a true hang (one that burns neither cost nor turns) is still
#: caught. It is deliberately NOT a latency gate. Provisioning / E2E / workspace
#: timeouts are unaffected — those legitimately catch I/O waste and live
#: elsewhere; this constant scopes strictly to the eval lane.
DEFAULT_WATCHDOG_SECONDS = 900

#: GENEROUS default per-run budget for the metered ``t3 eval run --backend api``
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
#: the DEFAULT — the metered ``t3 eval run --backend api`` lane and the benchmark
#: thread a generous cap so a finishing scenario is measured, not truncated. See
#: :data:`CleanRoomConfig.max_budget_usd` and ``METERED_DEFAULT_BUDGET_USD``.
MAX_BUDGET_USD = "0.10"
FALLBACK_MODEL = "claude-sonnet-5"
EMPTY_SETTINGS = '{"hooks":{}}'

#: Local-plugin path (relative to the teatree repo root) for the eval-only
#: skill-catalog fixture: synthetic ``SKILL.md`` stand-ins for names a
#: skill-routing scenario's prompt references that core does not itself ship —
#: a placeholder overlay's workspace/legal-entity skill, a companion language
#: bible, the review skill named without a leading slash. Registered ONLY for a
#: scenario that declares ``EvalSpec.available_skills`` (see
#: :func:`_skill_catalog_fixture_plugin`); every scenario that declares none
#: never loads it, so the isolation guarantee (no personal/project context bias)
#: is unchanged for the existing catalog. It carries no ``hooks.json`` of its
#: own, so loading it cannot resurrect the ``UserPromptSubmit`` skill-suggestion
#: hook the ``settings=EMPTY_SETTINGS`` isolation already suppresses — the exact
#: hook these scenarios' prompts say "did not fire" to force a genuine self-load.
_SKILL_CATALOG_FIXTURE_RELATIVE_PATH = ("evals", "fixtures", "skill_catalog")

#: The fixture plugin's registered name — MUST equal the ``name`` in
#: ``evals/fixtures/skill_catalog/.claude-plugin/plugin.json``
#: (pinned by ``test_plugin_name_constant_matches_the_fixture_plugin_json``).
#: The bundled ``claude`` CLI lists a plugin's skills under the plugin-qualified
#: ``<plugin>:<skill>`` key (verified against the binary: the ``system/init``
#: event exposes ``eval-skill-catalog:t3-widget``). The SDK ``skills`` filter
#: accepts either that qualified key OR the bare SKILL.md ``name`` (types.py:
#: "Names match the SKILL.md name / directory name, or plugin:skill"), so the
#: qualified form is the unambiguous canonical key (§8 identity-normalization) —
#: it matches the CLI's own listing exactly and stays correct if a future CLI
#: build ever tightens the filter to the qualified form only.
#: :func:`_qualify_catalog_skill` canonicalizes each declared name UP to it.
_SKILL_CATALOG_PLUGIN_NAME = "eval-skill-catalog"

#: Typed alias callers may ``raise``/``except`` against. The SDK raises a bare
#: ``Exception`` for the budget breaker, so the runner ALSO matches the message
#: substring — this alias only types the direct-raise path, never narrows it.
BudgetExceededError = RuntimeError


class ClaudeCliMissingError(RuntimeError):
    """Raised when ``claude`` is not on PATH while the all-skipped gate is armed.

    ``require_executed`` callers (the metered CI eval job) cannot tolerate a
    decorative skip: a missing binary means the metered backend can execute
    nothing, so the suite would report an all-skipped green. Raising here fails
    the job at the earliest point — before any scenario is graded.
    """


def resolve_claude_path(
    *,
    max_attempts: int = CLAUDE_RESOLVE_MAX_ATTEMPTS,
    backoff_seconds: float = CLAUDE_RESOLVE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Resolve the ``claude`` binary path, riding out a transient mid-run absence.

    ``shutil.which("claude")`` momentarily returns ``None`` when the bundled CLI
    auto-updates mid-run (the nvm symlink is swapped). A single miss must NOT red
    the rest of the batch, so this RE-PROBES up to *max_attempts* times with a
    short *backoff_seconds* pause between tries. The FIRST successful resolution
    returns immediately (no wasted sleeps); a binary that is still unresolved after
    every attempt returns ``None`` (the caller then skips or hard-errors per
    ``require_executed``) — the bounded loop never spins forever on a genuinely
    absent binary. *sleep* is injectable so the retry is testable without real time.
    """
    attempt = 0
    while True:
        path = shutil.which("claude")
        if path is not None:
            return path
        attempt += 1
        if attempt >= max_attempts:
            return None
        sleep(backoff_seconds)


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
    #: Programmatic sub-agent definitions (the SDK's ``ClaudeAgentOptions.agents``),
    #: sent over the initialize request so the ``Agent`` spawn tool is genuinely
    #: usable — the same way the real agent gets its sub-agents. The SDK documents
    #: ``agents`` as the way to programmatically define custom sub-agents the
    #: ``Agent`` tool can spawn. Defaults ``None`` (no sub-agents, the
    #: judge/non-delegation shape); the runner provisions a generic delegation
    #: subagent for a scenario whose toolset exposes the spawn tool
    #: (:func:`scenario_exposes_subagent_spawn`).
    agents: dict[str, AgentDefinition] | None = None
    #: Skill names to widen the simulated Skill-tool catalog with (the SDK's
    #: ``skills`` context filter), sourced from ``EvalSpec.available_skills``.
    #: Empty (the default) leaves ``ClaudeAgentOptions.skills``/``plugins`` at
    #: their untouched defaults, so a scenario declaring none is byte-identical
    #: to before this field existed — this is a WIDENING lever, never a
    #: narrowing one. Non-empty registers the eval-only fixture plugin
    #: (:data:`_SKILL_CATALOG_FIXTURE_RELATIVE_PATH`) so the named skills are
    #: genuinely discoverable, then filters the listing to exactly this set.
    skills: tuple[str, ...] = ()


def _qualify_catalog_skill(name: str) -> str:
    """Canonicalize a fixture-catalog skill name UP to the CLI's ``<plugin>:<skill>`` key.

    The plugin-qualified form is the unambiguous canonical key the CLI lists a
    plugin's skills under (see :data:`_SKILL_CATALOG_PLUGIN_NAME`). An
    already-qualified name (any name containing ``":"``) is returned unchanged,
    so the transform is idempotent.
    """
    if ":" in name:
        return name
    return f"{_SKILL_CATALOG_PLUGIN_NAME}:{name}"


def _skill_catalog_fixture_plugin() -> SdkPluginConfig:
    """The local-plugin config for the eval-only skill-catalog fixture.

    Resolved against :func:`_teatree_root`, not the process cwd, so it resolves
    correctly whether the eval CLI runs from teatree's own root or a scenario's
    isolated temp dir (the sub-agent-spawning lane's ephemeral checkout never
    reaches this — none of the ``available_skills``-declaring scenarios expose
    the ``Agent`` spawn tool).
    """
    path = _teatree_root().joinpath(*_SKILL_CATALOG_FIXTURE_RELATIVE_PATH)
    return {"type": "local", "path": str(path)}


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

    ``agents`` is sent over the SDK initialize request (NOT a CLI argv flag, so a
    sub-agent definition never blows ``ARG_MAX``). It is what makes the ``Agent``
    spawn tool genuinely usable: a delegation scenario exposes ``Agent`` in its
    allowlist AND ships a sub-agent definition, mirroring how the real agent gets
    its sub-agents. ``None`` (the default) is the judge/non-delegation shape.

    ``skills``/``plugins`` widen the simulated Skill-tool catalog for a scenario
    that declares ``config.skills`` (threaded from ``EvalSpec.available_skills``):
    the eval-only fixture plugin is registered and the listing filtered to
    exactly the declared names, each canonicalized UP to the plugin-qualified
    ``<plugin>:<skill>`` key the CLI actually lists them under
    (:func:`_qualify_catalog_skill` — a bare-name filter matches nothing). Empty
    ``config.skills`` (the default) renders ``skills=None`` and ``plugins=[]`` —
    the SDK's own untouched defaults, so a scenario declaring none is
    byte-identical to before this lever existed.
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
        agents=config.agents,
        permission_mode="bypassPermissions",
        max_turns=config.max_turns,
        max_budget_usd=config.max_budget_usd,
        model=config.model,
        fallback_model=FALLBACK_MODEL,
        effort=config.effort,
        skills=[_qualify_catalog_skill(name) for name in config.skills] if config.skills else None,
        plugins=[_skill_catalog_fixture_plugin()] if config.skills else [],
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


class ApiInProcessRunner:
    """Run an :class:`EvalSpec` via the in-process Agent SDK and capture tool calls."""

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def __init__(  # noqa: PLR0913 — each kwarg is one runner-construction knob (workspace / turns / require / budget / effort / credential-conflicts); the list mirrors ``make_runner``'s contract.
        self,
        *,
        workspace: Path | None = None,
        max_turns_override: int | None = None,
        require_executed: bool = False,
        max_budget_usd: float = float(MAX_BUDGET_USD),
        effort: EffortLevel | None = None,
        conflicting_vars: tuple[str, ...] = _DEFAULT_CONFLICTING_VARS,
    ) -> None:
        self._workspace = workspace or Path.cwd()
        self._max_turns_override = max_turns_override
        self._require_executed = require_executed
        self._max_budget_usd = max_budget_usd
        #: Lane-level representative reasoning effort. Applied when a scenario
        #: declares no ``model@effort`` of its own (a declared effort wins).
        self._effort = effort
        #: The SELECTED eval credential's conflicting vars — the credential the
        #: isolated child must NOT fall back to (the metered API key strips the
        #: OAuth token; the subscription OAuth strips the API key). ``make_runner``
        #: passes the resolved eval credential's ``spec.conflicting_vars``; the
        #: default preserves the pre-#2707-reversal metered strip for direct callers.
        self._conflicting_vars = conflicting_vars

    def _resolve_max_turns(self, spec: EvalSpec) -> int:
        """Override wins; else a clean-room budget is floored to :data:`CLEAN_ROOM_MIN_TURNS`."""
        if self._max_turns_override is not None:
            return self._max_turns_override
        if spec.lane == CLEAN_ROOM_LANE:
            return max(spec.max_turns, CLEAN_ROOM_MIN_TURNS)
        return spec.max_turns

    def run(self, spec: EvalSpec) -> EvalRun:
        # Resolve the abstract tier/phase to a concrete model id (a no-op when the
        # spec already carries a concrete ``model``, e.g. the matrix/--model lanes
        # set it upstream). The resolved id flows into _drive's variant parse, the
        # model-presence check, the ledger label, and the report.
        spec = dataclasses.replace(spec, model=resolve_eval_model(spec))
        if resolve_claude_path() is None:
            if self._require_executed:
                msg = (
                    "claude binary not on PATH (after bounded re-resolve) but --require-executed "
                    "is armed: the metered api backend can execute no scenario, so the suite would "
                    "report an all-skipped green. Provision the Claude CLI (and "
                    "ANTHROPIC_API_KEY) on the runner — sdk + require-executed must never "
                    "decoratively skip."
                )
                raise ClaudeCliMissingError(msg)
            return self._skip_run(spec, "claude binary not on PATH")

        clean_room_prompt = load_agent_definition(spec.agent_path, spec.agent_sections) + LIVE_ENV_FRAMING
        system_prompt = build_system_prompt(spec, clean_room_prompt=clean_room_prompt)
        max_turns = self._resolve_max_turns(spec)
        try:
            messages = asyncio.run(self._drive(spec, system_prompt=system_prompt, max_turns=max_turns))
        except TimeoutError:
            return self._terminal_run(spec, terminal_reason="timeout")
        except TerminalResultError as terminal:
            return self._terminal_capped_run(spec, terminal)
        except SuccessMislabelResultError as mislabel:
            return self._success_mislabel_run(spec, mislabel)
        return eval_run_from_messages(spec, messages)

    def _terminal_capped_run(self, spec: EvalSpec, terminal: TerminalResultError) -> EvalRun:
        """Grade a run the SDK terminated at a known cap (budget/max-turns).

        When the agent produced a trajectory before the cap, grade the REAL
        trajectory: build via :func:`eval_run_from_messages` so the matchers
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
        message_amount = budget_amount_from_message(str(terminal.cause))
        if not terminal.messages:
            # Floor the over-budget cost to the scenario's EFFECTIVE budget (its own
            # per-scenario override when set, else the run default) so a raised-cap
            # scenario reports the cap it actually ran under, not the shared default.
            effective_cap = spec.max_budget_usd if spec.max_budget_usd is not None else self._max_budget_usd
            cost = budget_floor_from_message(str(terminal.cause), cap=effective_cap)
            empty_cost = cost if terminal.terminal_reason == BUDGET_EXCEEDED_REASON else 0.0
            return self._terminal_run(spec, terminal_reason=terminal.terminal_reason, cost_usd=empty_cost)
        graded = eval_run_from_messages(spec, terminal.messages)
        cost = message_amount if message_amount is not None else graded.cost_usd
        return dataclasses.replace(
            graded,
            terminal_reason=terminal.terminal_reason,
            is_error=False,
            cost_usd=cost,
        )

    @staticmethod
    def _success_mislabel_run(spec: EvalSpec, mislabel: SuccessMislabelResultError) -> EvalRun:
        """Grade a finished SUCCESS the CLI mislabeled by exiting non-zero.

        The captured ``result`` event reads ``subtype="success"`` but carries a
        stray ``is_error=True`` (the CLI exited non-zero on the success subtype).
        Grade the REAL trajectory via :func:`eval_run_from_messages` so the
        matchers decide pass/fail, then clear ``is_error`` — exactly the correction
        :meth:`_terminal_capped_run` applies — so a finished, all-matchers-pass run
        is not forced to FAIL on the flag alone (:attr:`ScenarioResult.passed` fails
        on ``is_error`` BEFORE consulting matchers). The ``terminal_reason`` already
        reads ``success`` and is left untouched — this is a finished run, not a cap.
        """
        graded = eval_run_from_messages(spec, mislabel.messages)
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
        with self._resolve_eval_target(spec) as (workspace, cwd, env):
            options = build_sdk_options(
                CleanRoomConfig(
                    system_prompt=system_prompt,
                    workspace=workspace,
                    cwd=cwd,
                    env=env,
                    allowed_tools=spec.tools,
                    available_tools=compute_available_tools(spec),
                    disallowed_tools=compute_disallowed_tools(spec),
                    agents=build_delegation_agents(spec),
                    model=variant.model,
                    max_turns=max_turns,
                    effort=effort,
                    max_budget_usd=max_budget_usd,
                    skills=spec.available_skills,
                )
            )
            return await asyncio.wait_for(_collect(build_user_prompt(spec), options), timeout=watchdog)

    @contextmanager
    def _resolve_eval_target(self, spec: EvalSpec) -> Iterator[tuple[Path, str, dict[str, str]]]:
        """Yield ``(workspace, cwd, env)`` — ISOLATED to a throwaway for spawning scenarios.

        A non-spawning scenario keeps the existing clean-room shape: the
        configured ``workspace``, the :func:`isolated_claude_env` neutral cwd, and
        the personal-context-redirected env.

        A SUB-AGENT-SPAWNING scenario (:func:`scenario_exposes_subagent_spawn`)
        additionally runs against a per-run EPHEMERAL CHECKOUT: ``workspace`` (the
        SDK ``add_dirs`` grant) and ``cwd`` both point at the throwaway, and the env
        is overlaid by :func:`ephemeral_checkout_env` so ``import teatree`` and
        ``git`` resolve into the throwaway rather than the developer's real clone.
        Without this, a spawned sub-agent locates the real clone via the editable
        install + shared ``.git`` (a neutral cwd does NOT block that) and does
        destructive git work on it — the corruption this isolation prevents.
        """
        with isolated_claude_env(self._conflicting_vars) as (env, cwd):
            if spec.fixture:
                with provision_git_fixture(spec.fixture) as repo:
                    yield repo, str(repo), env
                return
            if not scenario_exposes_subagent_spawn(spec):
                yield self._workspace, cwd, env
                return
            with provision_ephemeral_checkout() as checkout:
                isolated_env = ephemeral_checkout_env(env, checkout)
                yield checkout, str(checkout), isolated_env

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
    :class:`TerminalResultError` for the runner to grade; a SUCCESS mislabeled as
    an error result (the CLI exited non-zero on a ``"success"`` subtype) is
    re-raised inside a :class:`SuccessMislabelResultError` so the runner grades the
    captured messages AND clears the stray ``is_error``; a metered-key CREDIT
    exhaustion (HTTP 400 "credit balance is too low" — the billed
    ``ANTHROPIC_API_KEY`` at $0) is re-raised as a :class:`CreditExhaustedError`
    so the batch fails LOUD with the console remediation instead of redding every
    remaining scenario behind an opaque error result; any other error re-raises
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
            raise SuccessMislabelResultError(messages=messages, cause=exc) from exc
        limit = classify_limit(str(exc))
        if limit is not None and limit.cause is LimitCause.API_CREDIT:
            raise CreditExhaustedError(limit.remediation) from exc
        reason = classify_terminal_error(str(exc))
        if reason is None:
            raise
        raise TerminalResultError(terminal_reason=reason, messages=messages, cause=exc) from exc
    return messages


def _teatree_root() -> Path:
    """Return the teatree repo root (parent of ``src/teatree``)."""
    return Path(__file__).resolve().parents[3]
