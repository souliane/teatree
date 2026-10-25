"""Agent mode, harness and dispatch settings declarations."""

from dataclasses import dataclass
from typing import ClassVar, Final

from teatree.config.agent_enums import AgentHarness, AgentHarnessProvider, parse_harness_name
from teatree.config.declaration import Category, SettingSpec, setting
from teatree.config.enums import Autonomy, Mode
from teatree.config.setting_parsers import (
    _parse_harness_skill_exclusions,
    _parse_header_map,
    _parse_str_list,
    _parse_strict_bool,
    _parse_strict_float,
    _parse_strict_int,
    _parse_strict_str,
)

#: Owner-chosen per-request output-token ceiling for the ``pydantic_ai`` harness
#: (:attr:`_ModeHarnessSettings.pydantic_ai_max_tokens`). Generous by design — a ceiling
#: only bills for tokens actually generated, and the real spend guards on this lane are the
#: watchdog cost ceiling and ``pydantic_ai_request_limit``, not this cap. Should a run STILL
#: hit it, the truncation is recorded FAILED AND escalated to the owner
#: (:func:`teatree.agents.runner_truncation.alert_owner_max_tokens_truncation`) so the ceiling can be
#: raised deliberately rather than failing silently. pydantic_ai's Anthropic binding otherwise
#: defaults to 4096, which truncates a long result envelope mid-JSON.
#:
#: The value is bounded ABOVE by the smallest per-model output limit in
#: :data:`teatree.agents.model_tiering.tier_models()`, NOT by the frontier model's: this is ONE
#: global ceiling merged into every request (:func:`teatree.agents.pydantic_ai_config.build_model_settings`)
#: whatever tier the dispatched phase resolved to, and the Anthropic Messages API rejects a
#: ``max_tokens`` above the addressed model's own limit with a 400 rather than clamping. The
#: shipped tiers (Opus / Sonnet) allow 128K output, but a tier overridden to Haiku caps at
#: 64K, so 64K is the largest ceiling every Claude tier model accepts. Raising past it
#: requires making the ceiling per-tier first. Safe at this size because the lane STREAMS its
#: provider request (``event_stream_handler`` in
#: :meth:`teatree.agents.pydantic_ai_session.PydanticAiHarnessSession.receive_response`), so a
#: long generation cannot trip the non-streaming request timeout.
PYDANTIC_AI_MAX_TOKENS_DEFAULT: Final[int] = 64000


@dataclass
class _ModeHarnessSettings:
    """Mode / autonomy + the two-layer agent-harness (runtime / transport / provider) selectors."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Agents", "Mode & harness")

    mode: Mode = setting(
        Mode.AUTO,
        spec=SettingSpec(
            help="publishing posture: auto pushes without waiting, interactive asks first", parse=Mode.parse
        ),
    )
    autonomy: Autonomy = setting(
        Autonomy.FULL,
        spec=SettingSpec(help="how far the factory may act without asking the operator", parse=Autonomy.parse),
    )
    harness_skill_exclusions: list[str] = setting(
        default_factory=list,
        spec=SettingSpec(
            help=("box-wide Claude Code or Codex skills removed from that harness until explicitly installed again"),
            parse=_parse_harness_skill_exclusions,
            box_global=True,
        ),
    )
    # Skill selectors are also read by the cross-cutting skill-supply inventory,
    # so they belong with the agent harness rather than below any one loop.
    scanning_news_skill: str = setting(
        "scanning-news", spec=SettingSpec(help="skill the news scanning loop runs", parse=_parse_strict_str)
    )
    eval_local_skill: str = setting(
        "running-evals", spec=SettingSpec(help="skill the local eval loop runs", parse=_parse_strict_str)
    )
    backlog_sweep_skill: str = setting(
        "sweeping-tickets", spec=SettingSpec(help="skill the backlog sweep loop runs", parse=_parse_strict_str)
    )
    dogfood_smoke_skill: str = setting(
        "dogfooding", spec=SettingSpec(help="skill the dogfood smoke loop runs", parse=_parse_strict_str)
    )
    # Layer 1 of the two-layer harness config model (#2887): which in-process
    # TRANSPORT an agent run uses — the transport that opens the agent session behind the
    # ``teatree.agents.harness.Harness`` protocol. ``claude_sdk`` (default, today's
    # behaviour) is the ``claude-agent-sdk`` backend; ``pydantic_ai`` (#2885) is the
    # generic OpenAI-compatible backend. The backend set is OPEN (#3157 E1):
    # this is a registry KEY, not a closed enum — an overlay registers a third
    # transport under the ``teatree.harnesses`` entry-point group and selects it here
    # by name (an unregistered name fails LOUD at dispatch, not at config parse). The
    # built-in ``AgentHarness`` values remain the two shipped keys. Per-overlay
    # overridable; ``T3_AGENT_HARNESS`` env wins.
    agent_harness: str = setting(
        AgentHarness.CLAUDE_SDK,
        spec=SettingSpec(help="which agent CLI teatree drives (claude, codex, ...)", parse=parse_harness_name),
    )
    agent_advisor_model: str = setting(
        "",
        spec=SettingSpec(
            help="advisor model for headless Claude CLI child spawns",
            parse=_parse_strict_str,
            category=Category.PERSONAL,
            box_global=True,
        ),
    )
    # Layer 2 of the two-layer harness config model (#2887): the provider/
    # credential an agent run authenticates with, CONSTRAINED by Layer 1
    # (``AgentHarnessProvider.valid_for(agent_harness)`` — see the enum
    # docstring for the full constraint table). Default ``None`` — NO explicit
    # pin: a ``ClaudeSdkHarness`` dispatch inherits the ambient environment
    # unchanged, so an
    # operator who never touches this setting is never forced through an eager
    # credential lookup they haven't configured. An explicit ``subscription_oauth``
    # forces the plan's OAuth token (stripping the API key) — the ``claude_sdk``
    # default STANCE once pinned. ``api_key`` forces the metered key — the
    # ``claude_sdk``-only opt-in. On the ``pydantic_ai`` lane the harness builder
    # branches on this field: ``anthropic_api`` selects the native Anthropic
    # Messages-API binding (real ``cache_control``), anything else the generic
    # OpenAI-compatible router binding. A Vertex Layer-2 provider is reserved and
    # carries no enum member yet. Per-overlay overridable;
    # ``T3_AGENT_HARNESS_PROVIDER`` env wins.
    agent_harness_provider: AgentHarnessProvider | None = setting(
        None,
        spec=SettingSpec(
            help=(
                "credential the harness authenticates with — under claude_sdk: plans "
                "only, API only, or plans first with automatic failover to the API; "
                "empty inherits the ambient one"
            ),
            parse=AgentHarnessProvider.parse,
            category=Category.PERSONAL,
            storage_parse=lambda value: None if value is None else AgentHarnessProvider.parse(value),
        ),
    )
    agent_route_paid_fallback: bool = setting(
        default=False,
        spec=SettingSpec(
            help="allow an ordered agent route to fall through to a metered lane", parse=_parse_strict_bool
        ),
    )
    # The EXPLICIT allowlist of model-id patterns eligible on a regulated lane
    # (matched case-insensitively as substrings). Empty means this installation
    # has no regulated model restriction; a nonempty list enforces membership.
    # Per-overlay overridable. The provider must also constrain routing handles.
    regulated_path_model_allowlist: list[str] = setting(
        default_factory=list, spec=SettingSpec(help="models allowed to touch a regulated path", parse=_parse_str_list)
    )
    # Per-run sequential-request cap for the ``pydantic_ai`` harness
    # (the metered-lane guardrail). Passed as pydantic_ai
    # ``UsageLimits(request_limit=...)`` on every ``PydanticAiHarnessSession`` run
    # so a cheap-model maker cannot drift on a long tool loop — the FSM already
    # chunks work into phases and the orchestrator re-dispatches, so a per-run cap
    # composes with orchestration rather than killing tasks. The default is a REAL
    # turn budget: a live Lane-B task runs ~16 model requests, so the earlier cap of
    # 5 refused mid-task before the run ever reached ``open()``. 40 clears that
    # measured reality with generous headroom; a positive caller ``max_turns`` (an
    # ``OneShotSpec`` cap, an eval override) still wins over it (``harness.py`` /
    # ``eval/pydantic_ai_runner.py``). Applies ONLY to the ``pydantic_ai`` harness
    # (the default ``claude_sdk`` harness is bounded by the loop watchdog instead),
    # so it is inert until an overlay opts into ``agent_harness=pydantic_ai``. ``0``
    # disables the cap (the escape hatch). Per-overlay overridable.
    pydantic_ai_request_limit: int = setting(
        40, spec=SettingSpec(help="requests one pydantic-ai run may make", parse=_parse_strict_int)
    )
    # Per-request output-token ceiling for the ``pydantic_ai`` harness, passed as the base
    # ``max_tokens`` ``ModelSettings`` key on every run (both the OpenAI-compatible and native Anthropic
    # bindings honour it). pydantic_ai's Anthropic binding otherwise defaults to 4096, which
    # truncates a long result envelope mid-JSON and destroys it. The default is the owner-chosen
    # :data:`PYDANTIC_AI_MAX_TOKENS_DEFAULT` (64000) — deliberately generous because a ceiling
    # only bills for tokens actually generated. Should a run STILL hit it, the truncation is
    # recorded FAILED AND escalated to the owner
    # (``teatree.agents.runner_truncation.alert_owner_max_tokens_truncation``) so the ceiling is raised
    # deliberately rather than failing silently. Keep it ≤ the SMALLEST output limit among the
    # models a dispatch can resolve to (this one value is merged into every request whatever
    # tier ran, and the Anthropic API 400s on a ``max_tokens`` above the addressed model's own
    # limit rather than clamping) — see :data:`PYDANTIC_AI_MAX_TOKENS_DEFAULT`. Applies
    # ONLY to the ``pydantic_ai`` harness, so it is inert until an overlay opts into
    # ``agent_harness=pydantic_ai``. ``0`` leaves the binding's own default. Per-overlay overridable.
    pydantic_ai_max_tokens: int = setting(
        PYDANTIC_AI_MAX_TOKENS_DEFAULT,
        spec=SettingSpec(help="token ceiling for one pydantic-ai request", parse=_parse_strict_int),
    )
    # The generic OpenAI-compatible backend (#3666) — where, which model, whose key.
    # A provider is ORDINARY CONFIGURATION here, never a code path: pointing the
    # ``pydantic_ai`` harness at a different OpenAI-compatible API is these settings,
    # not a new credential class.
    #
    # The endpoint. NO default is fabricated — a wrong hardcoded host would silently
    # route real spend elsewhere — so an empty value with no
    # ``OPENAI_COMPATIBLE_BASE_URL`` env fails loud when the lane is selected.
    # Per-overlay overridable; ``T3_OPENAI_COMPATIBLE_BASE_URL`` env wins.
    openai_compatible_base_url: str = setting(
        "",
        spec=SettingSpec(
            help="base URL of the OpenAI-compatible endpoint", parse=_parse_strict_str, category=Category.PERSONAL
        ),
    )
    # The model id requested from that endpoint. Empty (the default) falls back to the
    # ``pydantic_ai_tier_models()`` id for the dispatch's abstract tier. Overrides ONLY
    # the normalise-UP branch of ``resolve_pydantic_ai_model``, so an explicit
    # provider-native model pin still wins. Per-overlay overridable;
    # ``T3_OPENAI_COMPATIBLE_MODEL`` env wins.
    openai_compatible_model: str = setting(
        "",
        spec=SettingSpec(
            help="model name requested from the OpenAI-compatible endpoint",
            parse=_parse_strict_str,
            category=Category.PERSONAL,
        ),
    )
    # The NAME of the credential-store entry the API key is read from (e.g.
    # ``<provider>/<account>/api-key``) — an entry NAME, never a key value. The
    # ``OpenAICompatibleCredential`` has no built-in default, so this is the only store
    # source; empty (the default) means it resolves from ``OPENAI_COMPATIBLE_API_KEY``
    # alone and otherwise fails loud naming this setting. Per-overlay overridable.
    openai_compatible_credential_entry: str = setting(
        "",
        spec=SettingSpec(
            help="secret-store entry holding the OpenAI-compatible key",
            parse=_parse_strict_str,
            category=Category.SECRET,
        ),
    )
    # The ``x-lane`` header every request rides, so the endpoint's analytics — and any
    # routing rule keying on it — can tell the call-site lanes apart: ``factory``
    # (default — the headless factory dispatch), ``eval`` (the eval CI job, set via
    # ``T3_OPENAI_COMPATIBLE_LANE=eval`` in that job's env), and ``bulk`` (a secondary
    # overlay's cheap bulk legs). Resolved SYNCHRONOUSLY in ``resolve_harness``. Inert
    # until an overlay opts into ``agent_harness=pydantic_ai``. Per-overlay overridable;
    # ``T3_OPENAI_COMPATIBLE_LANE`` env wins.
    openai_compatible_lane: str = setting(
        "factory",
        spec=SettingSpec(help="which work lane routes to the OpenAI-compatible endpoint", parse=_parse_strict_str),
    )
    # Extra headers every OpenAI-compatible request carries beside ``x-lane`` (a router's
    # session-affinity and cost headers); ``{session}`` in a value becomes the run's session id.
    # Inert until an overlay opts into ``agent_harness=pydantic_ai``. Per-overlay overridable.
    openai_compatible_extra_headers: dict[str, str] = setting(
        default_factory=dict,
        spec=SettingSpec(
            help="extra headers sent to the OpenAI-compatible endpoint; {session} is the run id",
            parse=_parse_header_map,
            category=Category.PERSONAL,
        ),
    )
    # Absolute per-RUN watchdog ceilings for the headless ``claude_sdk`` lane (#882,
    # F9.5). ``LoopWatchdog.from_settings`` reads these through the DB-home config
    # tier. ``0`` disables a dimension —
    # matching the shipped-off turn/cost caps (only the generous runtime ceiling is
    # armed by default). Per-overlay overridable.
    watchdog_max_runtime_seconds: int = setting(
        3 * 60 * 60,
        spec=SettingSpec(help="seconds a watched run may take before the watchdog stops it", parse=_parse_strict_int),
    )
    watchdog_max_turns: int = setting(
        0, spec=SettingSpec(help="turns a watched run may take before the watchdog stops it", parse=_parse_strict_int)
    )
    watchdog_max_cost_usd: float = setting(
        0.0,
        spec=SettingSpec(
            help="dollars a watched run may spend before the watchdog stops it", parse=_parse_strict_float
        ),
    )
    # Per-TICKET cumulative cost cap for the agent lane (#885 / #398-4, F9.5).
    # ``TicketBudget.from_settings`` reads it through the DB-home config tier.
    # ``0.0`` disables the cap. Per-overlay overridable.
    ticket_budget_max_cost_usd: float = setting(
        0.0, spec=SettingSpec(help="dollars one ticket may cost before it is halted", parse=_parse_strict_float)
    )
    # Deterministic ceiling on how many sub-agents ONE headless run may spawn
    # (``agents/subagent_ceiling.py``). The runtime watchdog bounds a run's duration,
    # not its fan-out, so a wall clock cannot see a delegation runaway. ``0`` disables
    # the gate, matching the watchdog convention above. Per-overlay overridable.
    subagent_spawn_ceiling: int = setting(
        20, spec=SettingSpec(help="sub-agents one dispatch may spawn", parse=_parse_strict_int)
    )
    # How many envelope-less turn-ends ONE headless run may have refused before the
    # Stop gate (``agents/envelope_stop_gate.py``) stands down. The gate fails open
    # and never re-refuses a turn another Stop hook blocked; ``0`` disables it,
    # matching the ceilings above. Per-overlay overridable.
    envelope_stop_gate_refusals: int = setting(
        2,
        spec=SettingSpec(help="envelope-less turn-ends one run may have refused; 0 disables", parse=_parse_strict_int),
    )
    # Per-RUN turn ceiling pinned on the headless ``claude_sdk`` spawn
    # (``ClaudeAgentOptions.max_turns`` via ``agents/_runner_options``). Cache-read
    # cost is ``turns x context_size`` and each turn re-reads the run's whole context,
    # so the turn count is the multiplier on a dispatch's bill — a dimension the
    # wall-clock ``watchdog_max_runtime_seconds`` cannot see and the
    # ``watchdog_max_turns`` ceiling reads only from COMPLETED attempts, never the
    # in-flight one. The default clears a long healthy phase run with headroom while
    # bounding a run that has stopped converging. Reaching it is NOT silent: the CLI
    # ends the run with ``ResultMessage(subtype="error_max_turns")``, which
    # ``agents/runner`` records FAILED naming this setting AND escalates to the owner
    # (``agents/runner_truncation``), so the ceiling is raised deliberately rather
    # than the work being quietly truncated. ``0`` leaves the spawn uncapped (the
    # escape hatch, matching the ceilings above). Per-overlay overridable.
    agent_max_turns: int = setting(
        250, spec=SettingSpec(help="turn ceiling for one agent run", parse=_parse_strict_int)
    )
