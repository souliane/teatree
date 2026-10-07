"""TeaTree config dataclasses — ``UserSettings``, ``TeaTreeConfig``, ``OverlayEntry``.

The flat persisted schema and its field defaults. WHERE a value may come from and
HOW a stored one is coerced live in the sibling ``setting_registries``. Both are
re-exported from ``teatree.config`` so every ``teatree.config.<name>`` path stays
valid.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final

from teatree.config.agent_enums import AgentHarness, AgentHarnessProvider
from teatree.config.enums import Autonomy, Mode, PrReviewBackend, Wip
from teatree.config.mr_reminder import MrReminderConfig
from teatree.config.settings_loop_flags import _LoopFlagAndCredentialSettings
from teatree.config.settings_loop_owned import (
    _ArchReviewLoopSettings,
    _BacklogSweepLoopSettings,
    _DirectiveLoopSettings,
    _DogfoodLoopSettings,
    _DreamLoopSettings,
    _HousekeepingLoopSettings,
    _InboxLoopSettings,
    _IssueImplementerLoopSettings,
    _NewsLoopSettings,
    _ResourcePressureLoopSettings,
    _ReviewLoopSettings,
    _SnapshotWarmerLoopSettings,
    _TicketsLoopSettings,
)
from teatree.types import DEFAULT_MR_TITLE_REGEX, SpeakConfig

WRITE_CONCURRENCY_PER_CORE: Final = 0.5
WRITE_CONCURRENCY_PER_CORE_MIN: Final = 0.25
WRITE_CONCURRENCY_PER_CORE_MAX: Final = 2.0


@dataclass
class OverlayEntry:
    name: str
    overlay_class: str
    project_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def canonical_overlay_name(name: str) -> str:
        """The route/dedup key for an overlay identifier: ``name`` minus the ``t3-`` prefix.

        A TOML overlay table and the ``t3-``-prefixed entry point that registers
        it address the same overlay; this strip is the key under which CLI
        sub-apps are routed and deduplicated so the pair cannot register two
        sub-apps.

        This is the CLI-routing key only; stored overlay names use the full
        entry-point name.
        """
        return name.removeprefix("t3-")


@dataclass
class _WorkspaceCoreSettings:
    """Workspace root + the core engagement / identity flags.

    A private in-file group base (see :class:`UserSettings`). Pure data-declaration —
    no behaviour — so the flat persisted schema is preserved by inheritance.

    ``GROUP_PATH`` is where this base's fields render in the settings hierarchy —
    a ``ClassVar``, so it is declaration metadata and never a persisted field. It is
    the ONLY place this group is named: ``teatree.config.setting_groups`` reads it off
    the MRO, so a field added below is placed by it with nothing else to edit.
    """

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Workspace", "Engagement & identity")

    workspace_dir: Path = field(default_factory=lambda: Path.home() / "workspace")
    # #256 Default-OFF teatree engagement. When false (the default) a fresh
    # Claude session does NOT auto-engage teatree — no skill auto-suggest, no
    # PreToolUse load-block, no loop scheduling — and SessionStart shows a
    # one-line how-to-start advisory instead. The owner flips it true to
    # auto-activate every session. DB-home (DB-home cutover): the cold
    # SessionStart hook and the PreToolUse gates read it DB-ONLY pre-Django via the
    # Django-free ``cold_reader`` (``teatree_settings._cold_db_bool``) and the bash
    # ``statusline.sh._autoload_db_value`` (sqlite3 CLI); ``T3_AUTOLOAD`` env wins, a
    # ``[teatree] autoload`` TOML value is ignored on read. Explicitly calling
    # ``/teatree`` — or loading any ``t3:`` skill — engages teatree for the
    # session regardless of this default.
    autoload: bool = False
    contribute: bool = False
    excluded_skills: list[str] = field(default_factory=list)


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
#: :data:`teatree.agents.model_tiering.TIER_MODELS`, NOT by the frontier model's: this is ONE
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

    mode: Mode = Mode.AUTO
    autonomy: Autonomy = Autonomy.FULL
    harness_skill_exclusions: list[str] = field(default_factory=list)
    # Skill selectors are also read by the cross-cutting skill-supply inventory,
    # so they belong with the agent harness rather than below any one loop.
    scanning_news_skill: str = "scanning-news"
    eval_local_skill: str = "running-evals"
    backlog_sweep_skill: str = "sweeping-tickets"
    dogfood_smoke_skill: str = "dogfooding"
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
    agent_harness: str = AgentHarness.CLAUDE_SDK
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
    agent_harness_provider: AgentHarnessProvider | None = None
    # The EXPLICIT allowlist of model-id patterns eligible on a regulated lane
    # (matched case-insensitively as substrings). Empty means this installation
    # has no regulated model restriction; a nonempty list enforces membership.
    # Per-overlay overridable. The provider must also constrain routing handles.
    regulated_path_model_allowlist: list[str] = field(default_factory=list)
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
    pydantic_ai_request_limit: int = 40
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
    pydantic_ai_max_tokens: int = PYDANTIC_AI_MAX_TOKENS_DEFAULT
    # The generic OpenAI-compatible backend (#3666) — where, which model, whose key.
    # A provider is ORDINARY CONFIGURATION here, never a code path: pointing the
    # ``pydantic_ai`` harness at a different OpenAI-compatible API is these settings,
    # not a new credential class.
    #
    # The endpoint. NO default is fabricated — a wrong hardcoded host would silently
    # route real spend elsewhere — so an empty value with no
    # ``OPENAI_COMPATIBLE_BASE_URL`` env fails loud when the lane is selected.
    # Per-overlay overridable; ``T3_OPENAI_COMPATIBLE_BASE_URL`` env wins.
    openai_compatible_base_url: str = ""
    # The model id requested from that endpoint. Empty (the default) falls back to the
    # ``PYDANTIC_AI_TIER_MODELS`` id for the dispatch's abstract tier. Overrides ONLY
    # the normalise-UP branch of ``resolve_pydantic_ai_model``, so an explicit
    # provider-native model pin still wins. Per-overlay overridable;
    # ``T3_OPENAI_COMPATIBLE_MODEL`` env wins.
    openai_compatible_model: str = ""
    # The NAME of the credential-store entry the API key is read from (e.g.
    # ``<provider>/<account>/api-key``) — an entry NAME, never a key value. The
    # ``OpenAICompatibleCredential`` has no built-in default, so this is the only store
    # source; empty (the default) means it resolves from ``OPENAI_COMPATIBLE_API_KEY``
    # alone and otherwise fails loud naming this setting. Per-overlay overridable.
    openai_compatible_credential_entry: str = ""
    # The ``x-lane`` header every request rides, so the endpoint's analytics — and any
    # routing rule keying on it — can tell the call-site lanes apart: ``factory``
    # (default — the headless factory dispatch), ``eval`` (the eval CI job, set via
    # ``T3_OPENAI_COMPATIBLE_LANE=eval`` in that job's env), and ``bulk`` (a secondary
    # overlay's cheap bulk legs). Resolved SYNCHRONOUSLY in ``resolve_harness``. Inert
    # until an overlay opts into ``agent_harness=pydantic_ai``. Per-overlay overridable;
    # ``T3_OPENAI_COMPATIBLE_LANE`` env wins.
    openai_compatible_lane: str = "factory"
    # Extra headers every OpenAI-compatible request carries beside ``x-lane`` (a router's
    # session-affinity and cost headers); ``{session}`` in a value becomes the run's session id.
    # Inert until an overlay opts into ``agent_harness=pydantic_ai``. Per-overlay overridable.
    openai_compatible_extra_headers: dict[str, str] = field(default_factory=dict)
    # Absolute per-RUN watchdog ceilings for the headless ``claude_sdk`` lane (#882,
    # F9.5). ``LoopWatchdog.from_settings`` reads these through the DB-home config
    # tier. ``0`` disables a dimension —
    # matching the shipped-off turn/cost caps (only the generous runtime ceiling is
    # armed by default). Per-overlay overridable.
    watchdog_max_runtime_seconds: int = 3 * 60 * 60
    watchdog_max_turns: int = 0
    watchdog_max_cost_usd: float = 0.0
    # Per-TICKET cumulative cost cap for the agent lane (#885 / #398-4, F9.5).
    # ``TicketBudget.from_settings`` reads it through the DB-home config tier.
    # ``0.0`` disables the cap. Per-overlay overridable.
    ticket_budget_max_cost_usd: float = 0.0
    # Deterministic ceiling on how many sub-agents ONE headless run may spawn
    # (``agents/subagent_ceiling.py``). The runtime watchdog bounds a run's duration,
    # not its fan-out, so a wall clock cannot see a delegation runaway. ``0`` disables
    # the gate, matching the watchdog convention above. Per-overlay overridable.
    subagent_spawn_ceiling: int = 20
    # How many envelope-less turn-ends ONE headless run may have refused before the
    # Stop gate (``agents/envelope_stop_gate.py``) stands down. The gate fails open
    # and never re-refuses a turn another Stop hook blocked; ``0`` disables it,
    # matching the ceilings above. Per-overlay overridable.
    envelope_stop_gate_refusals: int = 2
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
    agent_max_turns: int = 250


@dataclass
class _LoopSettings:
    """WIP dial + loop cadence/runner + the worker admission gate."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "Cadence & throughput")

    # How much new work a loop tick admits at once — the bounded-WIP dial.
    # ``slow`` caps to one impl worker; ``medium`` is the conservative baseline
    # (NO orchestrator fan-out — only the intrinsic loop + PR sweep + per-overlay
    # ``max_concurrent_auto_starts`` provide throughput); ``full`` (the default)
    # arms the /t3:wip loop; ``boost`` keeps ``boost_concurrency = N`` workers
    # live, refilling the pool each tick as workers exit. Orthogonal
    # to ``mode``/``autonomy`` (those gate *whether* a publish proceeds; this
    # governs *how many* threads run) and never relaxes a safety gate.
    # Per-overlay overridable; ``T3_WIP`` env wins over both.
    wip: Wip = Wip.FULL
    # #3634 The wip dial's PHASE SPLIT, replacing the single blunt concurrency knob.
    # ``write_wip`` is how many implementation workers (coding / testing / reviewing)
    # run concurrently; ``merge_wip`` is the merge lane, single-flight by design so
    # the next PR always rebases against what just landed. A ``merge_wip`` above 1
    # forfeits that conflict-safety guarantee, so the resolver clamps it back to 1.
    # DB-home, per-overlay overridable; ``T3_WRITE_WIP`` / ``T3_MERGE_WIP`` env win.
    write_wip: int = 3
    merge_wip: int = 1
    # Loop tick interval in seconds (BLUEPRINT § 5.6). Default 12 minutes.
    loop_cadence_seconds: int = 720
    # The drain-then-deploy admission gate (rolling/zero-downtime deploy). Default
    # OFF: the worker admits new work normally. `t3 worker drain` flips it ON for the
    # deploy window so the claim/admission path admits ZERO new tasks — the CAS
    # `claim_next_pending` and the `_claimable_for_target` query both short-circuit —
    # while in-flight CLAIMED leases keep renewing and finish. It is READ only at the
    # claim chokepoint; it deliberately does NOT feed the worker supervisor's
    # zero-admitted stop condition, so quiescing never stops the supervisor or kills a
    # live sub-agent. The FRESH worker's init clears it so admission resumes.
    # DB-home (#1775): resolved from the `ConfigSetting` store (global + overlay rows)
    # + `T3_WORKER_QUIESCING` env; a TOML value is ignored on read. Set via `t3 worker
    # drain` (which writes it) or `config_setting set worker_quiescing`.
    worker_quiescing: bool = False


@dataclass
class _OnBehalfSettings:
    """Human-approval training wheels + the on-behalf post gate + bot→user notify flags."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Communication", "Posting on your behalf")

    # Training-wheel for `auto` overlays: when true, the loop autonomously
    # pushes and creates PRs but stops short of merging — merge requires a
    # human reaction (👍 or `/merge`). The user flips this off only once
    # comfortable (BLUEPRINT § 5.6.2). No effect in `interactive` mode,
    # where every publishing action prompts regardless. NOT tier-governed
    # (#3630): no `autonomy` value collapses it, so removing review before
    # merge is always a deliberate, separately-named opt-in.
    require_human_approval_to_merge: bool = True
    # Whether the standing grant may sign off a SUBSTRATE merge (#3223). Default
    # off: a substrate CLEAR (merge keystone, architecture spec, governance doc,
    # self-guardrail seam) PINGS-and-HOLDS for the owner's per-PR sign-off even at
    # `autonomy = full` — the #2727 safety posture. Turning this on lets
    # `_overlay_grants_standing_substrate_signoff` cover a substrate CLEAR on an
    # overlay standing at `autonomy = full` (the solo-owned tier), so the owner's
    # own green PRs self-authorize substrate merges the same way non-substrate
    # clears already do. This changes only WHO authorizes the sign-off; the
    # quality/safety floor (independent cold review, reviewed-SHA bind, CI-green,
    # not-draft, maker≠checker, anti-vacuity) is untouched and still runs. The
    # `full` tier gate is kept so a below-full overlay never self-merges substrate
    # even with this on. DB-home (#1775), per-overlay overridable.
    substrate_self_signoff: bool = False
    # The owner id the headless loop presents as the standing substrate merge
    # authorization (#3413). Empty (the default) preserves the hold-for-owner
    # posture verbatim — a substrate CLEAR PINGS-and-HOLDS and is never
    # auto-merged (invariant 4). Setting it to an owner id is the durable,
    # revocable delegation: the config WRITE is the human authorization. When
    # set, the `pr_sweep` scanner presents this id at merge time as the
    # `--human-authorized` a substrate CLEAR requires, and the keystone
    # (`_config_standing_substrate_delegation`) authorizes the merge ONLY when the
    # presented id still equals this configured value — sourced from config, never
    # a live CLI flag, so unsetting it revokes the delegation at the next merge.
    # Every gate still runs (green required checks, recorded merge_safe verdict,
    # clean rebase, draft-lock, maker≠checker, SHA-bind); this changes only WHO
    # supplies the substrate authorization (a standing config delegation vs. a
    # per-PR recorded human approval), and the merge is audited as config-sourced
    # (`MergeAudit.standing_delegation_by`) to stay distinguishable from an
    # interactive human authorization. DB-home (#1775), per-overlay overridable.
    substrate_auto_merge_authorized_by: str = ""
    # Per-(repo, ticket) open-PR budget: the max number of concurrently-open
    # (not-merged) PRs a single ticket may have in one repo. Enforced at the
    # core PR-creation seam by ``pr_budget_gate`` before a PR is opened. The
    # shipped default is ``1`` — "at most one open PR per repo per ticket" — so
    # the one-ticket-one-PR discipline holds out of the box (a stray second PR
    # for the same ticket in the same repo is a recurring cleanup cost, D9). Set
    # ``0`` to restore the unlimited opt-out. Constraint-as-data: the scope is a
    # per-overlay ``ConfigSetting`` row, never a branch in core code, so an
    # overlay wanting a different value needs no code change. DB-home (#1775);
    # per-overlay overridable.
    max_open_prs_per_repo_per_ticket: int = 1
    # Training-wheel for the `t3:answerer` capability (#670, resolving
    # #654 Open Question #3): when true, the agent drafts a reply to an
    # inbound question, DMs the user for approval, and posts only on
    # confirmation. Set false to let the agent post answers directly — a
    # deliberate opt-in the user flips only once comfortable with answer
    # quality. Per-overlay overridable (a trusted overlay can opt into
    # direct posting without flipping the global). Default on, mirroring
    # `require_human_approval_to_merge`.
    require_human_approval_to_answer: bool = True
    # Carve-out from the on-behalf pre-gate: actions in this allowlist resolve
    # to PROCEED even under a forbidding egress posture, because they are the user's
    # routine self-documentation on their OWN ticket (E2E evidence), not a
    # colleague-facing voice that needs the user's per-post approval. Default
    # includes ``post_e2e_evidence`` so the user never has to approve their own
    # evidence posts; clear the list (``on_behalf_auto_actions = []``) to
    # re-gate evidence under a forbidding posture. Per-overlay overridable; env
    # ``T3_ON_BEHALF_AUTO_ACTIONS`` (comma-separated) wins over both.
    on_behalf_auto_actions: list[str] = field(default_factory=lambda: ["post_e2e_evidence"])
    # Notion ids the integration token may write under; with none configured every write is refused.
    notion_write_allowed_roots: list[str] = field(default_factory=list)
    # Notion ids never written under, even when an allowed root sits above them.
    notion_write_denied_roots: list[str] = field(default_factory=list)


@dataclass
class _IdentityRoutingSettings:
    """Statusline chain, operator identity aliases, repo mode, and the missing-issue policy."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Communication", "Identity & routing")

    statusline_chain: list[str] = field(default_factory=list)
    # Usernames / handles that all map to the same human operator across
    # platforms (a GitHub login, a GitLab username, an internal handle).
    # Two consumers:
    # - The ticket-disposition scanner uses them to suppress the reassign
    #   signal when an issue is handed off between two of the operator's
    #   own identities — plumbing noise, not an actionable transition
    #   (souliane/teatree#975).
    # - The loop's PR/MR scanners union-query each alias so cross-forge
    #   work (e.g. multiple GitHub logins under one PAT, GitHub vs GitLab
    #   handles for the same human) surfaces in the statusline (#976).
    # Default empty preserves legacy single-identity behaviour. Per-overlay
    # overridable so a tracker-scoped overlay can carry tracker-specific
    # handles without flipping the global default.
    user_identity_aliases: list[str] = field(default_factory=list)
    # Solo vs collaborative working mode (issue #550 item 4). Empty string
    # = auto-detect from `git shortlog` history (see teatree.repo_mode);
    # an explicit "solo" / "collaborative" pins the verdict and bypasses
    # detection. Consumed by skills via `t3 tool repo-mode` so the
    # ask-first-vs-fix-proactively decision lives in one place, not in
    # every skill.
    repo_mode: str = ""


@dataclass
class _ArchitecturalReviewSettings:
    """The skill the periodic architectural review dispatches — its cadence is the loop's."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Architectural review")

    # #1136 / #1152 Periodic architectural-review scanner — CORE always-on (not
    # per-overlay opt-in). The cadence applies uniformly to every overlay's
    # worktrees because it is a teatree-platform behaviour; the ``arch_review``
    # Loop row (and any preset masking it) is what turns the scanner off.
    architectural_review_skill: str = "architectural-review"


@dataclass
class _ReviewGateSettings:
    """Review-phase evidence gates + review-board admission + the E2E confidence bar."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Review")

    # Identities this deployment additionally trusts as INDEPENDENT checkers (#4241),
    # on top of the harness's recognised reviewer-role tokens. maker≠checker is a
    # fail-CLOSED allowlist, so a reviewer whose handle carries no role word — every
    # human, an external review service, a novel agent role — is refused until it is
    # named here (the owner's own `user_identity_aliases` are unioned in for free, so
    # the common case needs no configuration). Widening a fail-closed trust boundary is
    # itself an authorization, so the key is a `SAFETY_POSTURE_KEYS` member the MCP
    # write surface refuses. DB-home (#1775), per-overlay overridable.
    independent_reviewer_identities: list[str] = field(default_factory=list)

    # #1539 Per-ticket deep-review skill. Empty = opt-in unset: the
    # reviewing-phase evidence gate (``teatree.core.gates.review_skill_gate``) is
    # a NO-OP, so projects that do not configure a review skill keep
    # recording the ``reviewing`` attestation unchanged. When set (e.g.
    # ``ac-reviewing-codebase``), ``lifecycle visit-phase <id> reviewing``
    # refuses to record the phase without durable evidence the skill ran.
    # Distinct from ``architectural_review_skill`` (the periodic cadence
    # scanner) — this one gates a single ticket's reviewing attestation.
    review_skill: str = ""
    # Additional skills whose recorded run ALSO satisfies the reviewing-phase
    # evidence gate — substitutes for ``review_skill``, never a bypass. An
    # overlay that accepts a second deep reviewer (a different model's, say)
    # names it here so either recorded run passes while a run of NEITHER stays
    # refused. Empty default = the single-skill behaviour; alternates alone never
    # arm the gate, which stays keyed on ``review_skill`` being set.
    review_skill_alternates: list[str] = field(default_factory=list)
    # How long a review backend stays parked after its account ran out of quota
    # (``ReviewBackendCooldown``). Long enough that `auto` stops re-probing every
    # tick — each probe costs a burned dispatch to rediscover the same exhaustion —
    # and short enough that a refilled window is picked up the same working day.
    review_backend_cooldown_hours: int = 6
    # Which reviewer executes the self-authored-PR cold review. ``auto`` (the
    # default) resolves per tick — codex when its binary is present and not
    # cooling down from a quota exhaustion, else claude — so a codex-less box or
    # an exhausted account keeps getting reviews. An explicit ``claude`` /
    # ``codex`` pin is honoured as written and never silently degrades.
    pr_review_backend: PrReviewBackend = PrReviewBackend.AUTO


@dataclass
class _MergeGateSettings:
    """The gates a ticket must clear to reach MERGED — evidence, plan, repro, debt."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Merge & done")

    # The branch-protection required-status-check contexts the operator KNOWS must
    # gate a merge on this overlay's repos (e.g. ``["test (3.13)"]``). A fail-closed
    # floor: when the forge reports a DETERMINATE-EMPTY required set (branch
    # protection removed or never configured) while this floor is non-empty, the
    # keystone CI verdict fails closed to ``failed`` — a removed branch-protection
    # gate can no longer classify as "all checks passed / green". Default empty =
    # NO-OP (a genuinely gate-less repo still merges); the operator opts in per
    # overlay once their repos carry required checks. Per-overlay overridable.
    expected_required_contexts: list[str] = field(default_factory=list)


@dataclass
class _CriticGateSettings:
    """The user-proxy critic's posture, the send-proxy destination guard, the bulk-close cap."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Quality", "Critic & send proxy")

    # #117 send-proxy — every outbound artifact (Slack post/DM/react, forge
    # PR/MR/issue comment) routes through ``teatree.core.send_proxy``, which
    # redaction-scans the payload and checks the destination against
    # ``send_proxy_allowlist``. It refuses a non-allowlisted destination and
    # redacts matching terms from the payload.
    # The per-overlay destination allowlist: ``fnmatch`` globs over the raw
    # destination (Slack channel id or ``org/repo`` slug) and the
    # channel-qualified ``<channel>:<destination>`` form.
    # Empty by default. The user's own DM is always allowed (never-lockout carve-out),
    # so an empty allowlist can never gate the bot→user notify path. Per-overlay overridable.
    send_proxy_allowlist: list[str] = field(default_factory=list)
    # PR-08 No-bulk-close threshold: a single command/agent action closing more
    # than this many tickets/MRs is refused without an explicit per-item
    # confirmation token (``bulk_close_gate``). A close of ≤ threshold items is
    # always allowed. Per-overlay overridable.
    bulk_close_threshold: int = 5


@dataclass
class _ScannerSettings:
    """What the loop fan-out sweeps, and whether it may start at all — box facts, not one loop's."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Loops", "Fan-out & admission")

    #: Which overlays the full-fleet scanners sweep; empty sweeps every registered overlay.
    #: A property of the BOX, not of whichever preset is active — it answers "whose repos
    #: does this factory look after", and that does not change when the operator goes AFK.
    scanner_overlay_scope: list[str] = field(default_factory=list)
    schema_readiness_gate_enabled: bool = True


# The regenerable cache dirs auto-purged at CRITICAL disk pressure (the
# ``disk_cache_allowlist`` default). A module constant so the field default stays a
# single line; ``.copy`` gives each settings instance its own list.
#
# ``~/.cache/prek`` and ``~/.cache/uv`` are deliberately NOT here: prek's per-hook
# environments have unknown rebuild cost (opt in explicitly), and uv's cache is
# already pruned safely by the freeing ladder's own ``uv cache prune`` step. An
# entry that names nothing on this host is reported ABSENT in the plan rather
# than counted as a 0.00 GB purge (#3852).
_DEFAULT_DISK_CACHE_ALLOWLIST = [
    "~/.cache/pre-commit",
    "~/.cache/puppeteer",
    "~/.cache/codex-runtimes",
    "~/.cache/go-build",
]


@dataclass
class _ResourcePressureSettings:
    """The free-disk bands auto-free acts on, what it may reclaim, and the intake arm it drives."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Infrastructure", "Resource pressure", "Thresholds & cadence")

    # #3992 The resource loop derives issue-intake concurrency from observed headroom
    # instead of it being a hand-set constant. Flipping this OFF is the kill-switch:
    # ``issue_implementer_max_concurrent`` is then used verbatim, as before.
    # Allow-LIST only (never a denylist): exactly these regenerable cache dirs
    # are auto-purged at CRITICAL. ``uv`` is handled via ``uv cache prune``.
    # ``~/.cache/prek`` and ``~/.claude/projects`` are deliberately absent —
    # the latter is hard-protected even if a user adds it.
    disk_cache_allowlist: list[str] = field(default_factory=_DEFAULT_DISK_CACHE_ALLOWLIST.copy)
    # Measures ABSOLUTE free bytes (``os.statvfs``) — never percent-of-nominal, which
    # the APFS shared-container total misleads about.
    disk_crit_free_gb: float = 10.0
    disk_warn_free_gb: float = 25.0
    # #4244 The retention policy for the checkout pool: a ``.venv`` untouched for
    # this long is evicted as the pure cache it is (``uv sync`` rebuilds it), so
    # the pool's steady state is roughly one venv per checkout worked inside the
    # window rather than one per checkout ever created. Not a destructive lever —
    # a build product holds no work — but a live process inside a checkout always wins.
    artifact_idle_days: float = 2.0
    # #4580 A process group whose leader is gone is reported once it has run this long
    # and is still burning. The only knob: the age is what an operator retunes when the
    # finding nags, while the burn-rate floor and the never-reap protect-list stay in code
    # — an operator-emptiable safety list is a footgun with no upside.
    orphan_group_min_age_hours: int = 6


@dataclass
class _RetentionSettings:
    """Task-sweep, stack concurrency, control-DB retention windows, and session staleness."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Infrastructure", "Retention & sweeps")

    # Read by the dream pass, the backlog-sweep scanner and retro, so it belongs to no one loop.
    dream_umbrella_url: str = "https://github.com/souliane/teatree/issues/2663"

    # The branch every shipped PR targets when the ticket does not name one
    # itself. Empty (the default) keeps the historical behaviour — the repo's
    # own default branch. Set it when a whole line of work stacks onto ONE
    # long-lived integration branch instead of ``main``: every PR then targets
    # that branch, and ``run_branch_currency_gate`` merges it into each
    # worktree before shipping, so a merge into the integration branch
    # propagates to the branches still in flight. The repo-keyed
    # ``Ticket.extra['target_branch'][repo_slug]`` override still wins, and a
    # branch that IS the configured target falls back to the repo default so
    # the integration branch itself never targets itself.
    target_branch: str = ""
    # #1397 Cap on concurrent locally-running stacks for a single overlay.
    # Each running worktree (``services_up``/``ready``) holds docker
    # containers, browsers, language servers, and CI processes — on a
    # memory-constrained host (one OOM observed 2026-05-27 when two stacks
    # ran in parallel), one stack at a time is the workable limit. The
    # ``t3 <overlay> worktree start`` / ``workspace start`` gate refuses to
    # advance a second stack into ``SERVICES_UP`` while another is already
    # there, naming the blockers and pointing at ``worktree teardown``.
    # Default ``1``: for unattended 24/7 headless operation a single in-flight
    # local stack avoids merge conflicts against ``main`` and conserves the
    # Anthropic 5h token window. Set ``0`` to restore the legacy unbounded
    # behaviour, or any higher positive integer to raise the cap. Per-overlay
    # overridable: a heavy overlay can cap to ``1`` while a cheap dogfood
    # overlay stays unbounded (``0``).
    max_concurrent_local_stacks: int = 1
    # #3693 retention window for the high-churn ``TaskAttempt`` table. A ``prune``
    # deletes rows OLDER than the window whose owning ticket/task is TERMINAL —
    # never a live/in-flight row, and never within the window. Days, not a byte
    # ceiling: age is the operator-legible, safe-by-construction lever. It
    # defaults to a conservative 30 so a fresh install never prunes recent history;
    # ``0`` disables that table's pruning entirely. Per-overlay overridable.
    task_attempt_retention_days: int = 30
    # #3871 kill switch for the FSM-audit-trail lane. There is no window: the lane's
    # trigger is the ticket CLOSING, not a row aging, and what it removes is decided
    # per row — a ``from_state == to_state`` row records no edge, so it is not history
    # and a reopened ticket does not need it. Every real state edge is kept for as
    # long as the ticket exists (~410 rows on the measured box), so no age bound
    # applies to them. Per-overlay overridable.
    # #3871 window for ``django_tasks_db``' own ``DBTaskResult`` table. The delete is
    # the library's shipped ``prune_db_task_results`` command; this is only how far
    # back it is told to go. Short because nothing in teatree reads a FINISHED
    # result row — every consumer filters on READY or RUNNING — so a finished row is
    # pure post-mortem material, and the table takes ~400k of them a day. 1 day
    # matches the cadence the maintenance chain already ran at. ``0`` disables the
    # lane (the library's own floor is 1 day; sub-day retention is not expressible).
    task_result_retention_days: int = 1
    # #4165 agent-scratch retention under the temp root. On this box ``/tmp`` is a
    # RAM-backed tmpfs, so week-old sqlite/venv scratch is not idle disk — it is
    # 28% of the working memory pool, permanently. Ships OFF (``0``) because the
    # lane is a recursive delete and every destructive lever defaults OFF; 3 days
    # is the window the manual reclaim used, and arming it needs a venue whose
    # liveness probe can actually resolve host fds. The root is blank by default
    # and auto-resolves to the host view (``/host-tmp``, paired with
    # ``/host-proc``) when the deployment mounts it, else this venue's own
    # ``/tmp`` — a containerised sweep of the container's own temp root reclaims
    # nothing of what actually fills the box. Per-overlay overridable.
    scratch_retention_days: int = 0
    scratch_sweep_root: str = ""
    # Fail-safe staleness bound on the OPEN-Session liveness signal every reaper
    # consults (``Ticket.has_active_work``). An agent that crashed without closing
    # its Session would otherwise pin its ticket — and so its worktree, its
    # ``max_concurrent_local_stacks`` slot, and ``workspace relocate`` — busy
    # forever. An open Session whose last recorded activity (its own
    # ``started_at``, its tasks' heartbeats, its attempts' start times) is older
    # than this many hours is NOT live. 12h is 4x the ``watchdog_max_runtime_seconds``
    # hard cap on a single agent run, so it cannot mask real in-flight work — and
    # an active (PENDING/CLAIMED) task keeps a ticket busy with NO time bound at
    # all, independently of this. ``0`` disables the bound (every open Session is
    # live). Per-overlay overridable.
    session_stale_after_hours: int = 12


@dataclass
class _ProvisioningSettings:
    """Provisioning timeouts / concurrency + the idle-stack, stale-stack, and queue reaper knobs."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Infrastructure", "Provisioning")

    # #2220 Hard ceiling (seconds) for one long-blocking provisioning subprocess
    # — a DSLR snapshot restore, ``migrate``, or a ``--create-db`` test-DB
    # rebuild. On exceeding it the step ABORTS with an actionable error AND
    # fires a loud out-of-band user alert, instead of grinding silently for an
    # hour (the recurring "frozen sub-agent" symptom, e.g. a forked migration
    # graph). The default is generous (30 min) so a healthy restore+migrate
    # never trips it; a forked graph or a true hang blows past it and gets
    # aborted+alerted. A non-positive value degrades to the default — the
    # "never hang" invariant cannot be configured away. Per-overlay overridable.
    provision_step_timeout_seconds: int = 1800
    # Parallel ticket-workspace provisioning speed + resource-aware admission.
    # A fast step (symlinks, settings, a compose override) defaults to this
    # short ceiling; only a step explicitly marked ``ProvisionStep.heavy``
    # (a DB import, a frontend build) keeps ``provision_step_timeout_seconds``.
    # Per-overlay overridable.
    provision_fast_step_timeout_seconds: int = 120
    # nCPU-derived default concurrency cap for the bounded subprocess pool
    # ``workspace provision`` runs worktrees under. ``0`` (the default)
    # auto-derives from the host's ``os.cpu_count()`` at each read
    # (:func:`teatree.utils.ram_probe.default_provision_concurrency`); a
    # positive value pins an explicit cap. Per-overlay overridable.
    provision_max_concurrency: int = 0
    # #3644 The adaptive admission governor's default-ON flag. When true, every
    # dispatcher consults ``teatree.core.admission_governor`` before admitting work, so
    # token quota (primary) and machine load (secondary) bound concurrency and the
    # static concurrency settings become CEILINGS rather than targets. Setting it false
    # is the KILL-SWITCH and the rollback lever: admission reverts byte-for-byte to the
    # pre-governor static behaviour. Per-overlay overridable.
    # #4508 The admission-pressure scalar at which the EXPENSIVE agent class is refused
    # while the CHEAP review/ship lanes keep draining. 1.0 collapses SHED into HALT —
    # the rollback lever, restoring pre-#4508 admission byte-for-byte. Clamped into
    # [DEGRADE_AT, HALT_AT] on read so a typo cannot wedge the lane. Per-overlay overridable.
    admission_pressure_shed_at: float = 0.9
    # Box-global: the reader sizes this box's cores, so a per-overlay row cannot be honoured.
    admission_write_concurrency_per_core: float = WRITE_CONCURRENCY_PER_CORE
    # #4816 Whether the TOKEN brakes (the subscription quota family and the metered
    # one) apply at all. False drops both and leaves load + memory, so an operator
    # standing down a quota signal their lane does not answer to keeps the brakes that
    # protect the box.
    # Per-overlay overridable.
    # #4816 The metered lane's own spend ceiling, in TOKENS over
    # ``metered_spend_window_hours``. Tokens because they are MEASURED; the lane's
    # cost_usd is price-table arithmetic whose error is unknown. Ships 0 = UNSET: the
    # operator's provider-side cycle limit is not readable from here and teatree must
    # not invent a budget, so the metered brake is inert until this is set.
    # Per-overlay overridable.
    metered_token_ceiling: int = 0
    # #4816 The window ``metered_token_ceiling`` is measured over. Separate from the
    # amount because how much and over how long are two independent operator facts,
    # and the provider's cycle boundaries are not discoverable from teatree's data.
    # Per-overlay overridable.
    metered_spend_window_hours: int = 24
    # #4098 How many CHEAP headless phase agents (reviews, review requests, ship/merge,
    # short assessors — ``teatree.core.modelkit.phases.CHEAP_PHASES``) may occupy the
    # lane while the governor brakes the EXPENSIVE class. Those phases are what RETIRE
    # work, so refusing them on the load their coding siblings created removed the only
    # relief available and held the brake on for hours. They are cheap by comparison,
    # not free — one still gets a shell and can still run a suite — so this number is
    # small on purpose: it is the bound, not the exemption, that makes the class safe.
    # The exemption is from the MACHINE brake only; a spent token budget still refuses
    # every class. ``0`` disables the exemption entirely (cheap is braked exactly like
    # expensive): the rollback lever. Per-overlay overridable.
    cheap_phase_admission_ceiling: int = 2
    # #4374 How many of the governor's slots only the DRAINING class may occupy. The
    # ceiling above bounds how much of the box the cheap class may take and says nothing
    # about how much is kept FOR it, so expensive work filled every slot and zero reviews
    # ran — the #4098 outcome by a different route. Reviewing and shipping RETIRE pull
    # requests where coding CREATES them, so with capacity allocated purely first-come the
    # producing side can occupy the whole factory. Clamped to at most ``ceiling - 2``, so
    # the expensive class always keeps two slots and a fat-fingered value can never stop the
    # factory writing code — two rather than one because a 4-core box's ceiling is 2, where a
    # ``ceiling - 1`` clamp leaves a SINGLE expensive slot (#4407). ``0`` restores the
    # pre-#4374 first-come allocation: the rollback lever. Per-overlay overridable.
    drain_slot_reservation: int = 1
    # #4163 RAM one pytest-xdist worker is sized at when the governor derives the
    # per-agent worker cap. Measured p90 worker RSS from the kernel OOM log,
    # 2026-07-21..08-04: p50 0.65 GB, p90 1.24 GB, max 23.1 GB. The p90 is the sizing
    # point — the max is one pathological run, not a budget, and sizing at the p50 is
    # what put 16 workers x 1.24 GB = 19.8 GB against 19.7 GB usable. A non-positive
    # value drops the memory term, leaving the cores-derived bound: the rollback lever,
    # and the same bounded fail-safe an unreadable probe takes. Per-overlay overridable.
    test_worker_ram_gb: float = 1.25
    # Repos that are ONE branch wide while listed — ``<repo-slug>=<branch>`` entries.
    # While a repo is listed, provisioning a worktree on any OTHER branch is
    # refused, as are the raw ``git worktree add`` / ``checkout -b`` / ``switch
    # -c`` / ``branch <name>`` / non-pinned ``push`` forms
    # (:mod:`teatree.core.gates.single_branch_repo_guard`). The case it exists for
    # is a fork bootstrap: the repo's whole reviewed history is in flight behind
    # one open PR, so there is nothing to base a second branch ON, and the
    # measured cost of leaving that to prose was 31 worktrees across 37 branches
    # on two repos, two of them redoing a fix already in flight. Cleanup verbs
    # (``worktree list/remove/prune``, ``branch -D``) stay allowed — they are how a
    # repo that already sprawled gets back into line. REMOVING an entry is how the
    # rule ends when that PR merges. Per-overlay overridable.
    # Its kill-switch (``single_branch_repo_gate_enabled``) is a COLD-HOOK setting
    # rather than a field here, for the same reason ``main_clone_guard_gate_enabled``
    # is: the Bash seam reads it from a cold hook with no importable teatree.
    single_branch_repos: list[str] = field(default_factory=list)
    # RAM-used-percent ceiling above which a new provision is HELD (queued,
    # not started) rather than admitted, so a cold multi-repo provision never
    # pushes the host into OOM. 75 rather than the self-improve budget gate's
    # ``DEFAULT_RAM_USED_CEILING_PCT`` (85): provisioning arrives in bursts a
    # per-sample gate reads too late, and every live box already pins 75.
    # Per-overlay overridable.
    provision_ram_ceiling_percent: int = 75
    # A provision whose total duration exceeds this many seconds fires a
    # best-effort out-of-band user alert — a regression in provisioning speed
    # must never be silently absorbed. Per-overlay overridable.
    provision_slow_threshold_seconds: int = 600
    # #2190 Idle-stack reaper — a loop scanner that stops the docker stack of
    # an IDLE locally-running worktree (``services_up``/``ready``) and demotes
    # it to ``provisioned`` (REVERSIBLE: DB + worktree preserved), freeing the
    # host's RAM and a ``max_concurrent_local_stacks`` slot. Idle = no active
    # session/task on the ticket AND ``last_used_at`` older than
    # ``idle_stack_idle_minutes`` AND not the currently-active worktree AND no
    # active-delivery lease / recent E2E run / explicit pin (#2227).
    # Fail-safe: uncertainty ⇒ KEEP. All knobs are per-overlay overridable.
    idle_stack_idle_minutes: int = 30
    # #2227 Recency window for the E2E-run KEEP guard: a worktree whose
    # ``Worktree.last_e2e_run`` is within this many minutes is the live target of
    # in-flight evidence work and is never reaped, even when otherwise idle.
    idle_stack_e2e_recent_minutes: int = 60
    # #2207 Stale-stack reaper — tears down docker compose stacks that NO
    # Worktree row owns (hand-rolled test stacks, failed-teardown leftovers)
    # once their newest container lifecycle event (created/started/finished)
    # is older than this many minutes. Age-keyed so a parallel session's
    # fresh manual stack is never reaped; an unknown age fails safe (keep).
    # Runs automatically before ``worktree start`` / ``workspace start`` /
    # ``workspace provision`` and on demand via
    # ``t3 <overlay> workspace reap-stale``. ``0`` disables the sweep; a test
    # that must not reach the developer's real docker daemon sets it to ``0``
    # rather than relying on the default, which has been ON since #2207 landed.
    # Per-overlay overridable.
    stale_stack_min_age_minutes: int = 240
    # #2190/#44 Acquisition queue — when ``worktree start`` / ``workspace
    # start`` hits the cap, it reaps idle, retries, then ENQUEUES (no
    # SystemExit). A loop scanner drains the queue each tick with a
    # Fibonacci-minute backoff, never tearing down another ticket's stack.
    # `MAX_QUEUE_ATTEMPTS` caps the Fibonacci retries before a queued request
    # is marked DEAD and surfaced.
    # fnmatch globs of branch names ``clean-all`` must NEVER reap even when the
    # squash-merge classifier says shipped — never-merge dev overrides, long-lived
    # spikes. Matched against the full branch name. Default empty: nothing
    # protected beyond the data-loss guards. Per-overlay overridable.
    clean_ignore: list[str] = field(default_factory=list)


@dataclass
class _PrePublishGateSettings:
    """Slack voice, speak/mr-reminder, the pre-publish gate kill-switches, repo patterns, review batching."""

    GROUP_PATH: ClassVar[tuple[str, ...]] = ("Gates", "Pre-publish")

    # #2060 The resolved speak config — a local playback enum (off/dm/all) + a
    # slack bool. DB-home (#1775, DB-home cutover): stored as a JSON dict
    # ConfigSetting (``parse_speak_setting``), rebuilt bespoke by the resolver; the
    # cold Stop hook reads it via ``cold_reader``. See :class:`SpeakConfig`.
    speak: SpeakConfig = field(default_factory=SpeakConfig)
    # The resolved slug→channel routing table for the cross-repo "my open MRs"
    # reminder; empty default keeps it inert. DB-home (#1775): stored as a JSON dict
    # ConfigSetting (``parse_mr_reminder_setting``), rebuilt bespoke by the resolver.
    mr_reminder: MrReminderConfig = field(default_factory=MrReminderConfig)
    # #1398 Pre-publish close-trailer scanner. fnmatch patterns over
    # ``namespace/repo``: when an MR/PR target repo matches one of these
    # patterns and the body carries a ``Closes|Fixes|Resolves`` trailer,
    # the trailer line is silently stripped before publishing. Default
    # empty preserves legacy behaviour. DB-home (#1775); set via
    # ``t3 <overlay> config_setting set ban_close_trailers_on_namespaces``;
    # the TOML value is ignored on read.
    ban_close_trailers_on_namespaces: list[str] = field(default_factory=list)
    # Ceiling for the nag's Fibonacci re-ask backoff. Uncapped, the sequence runs past
    # the point where a reminder still reads as one — a request nobody answered would
    # go quiet for months instead of settling into a monthly rhythm.
    review_nag_max_interval_days: int = 30
    # A Slack user-group id, or a handle resolved as a user group then a user. Empty re-asks with no mention.
    review_nag_reask_mention: str = ""
    # Repo patterns whose merge requests need no review request: the user asks for
    # review in person there, so a posted request is noise a colleague has to dismiss.
    # Matched by ``teatree.core.review.repo_exemption`` on the same host-stripped
    # leading-segment-prefix grammar ``slug_namespace_matches`` uses. This is the PIN layer over
    # the overlay's derived ``review_exempt_repo_slugs()`` and it wins in BOTH
    # directions — an entry ADDS an exemption, a ``!``-prefixed one SUBTRACTS one the
    # overlay declared, so a changed policy is a config edit rather than a merge. The
    # deepest matching pattern decides and a tie does not exempt. Empty default =
    # INERT (no repo is exempt). Per-overlay overridable (DB-home).
    review_exempt_repos: list[str] = field(default_factory=list)
    # Whether a review-EXEMPT merge request still gates its work group's readiness.
    # ``True`` is the conservative reading — an exempt member keeps holding the group,
    # so the bias is toward NOT broadcasting a partial batch.
    review_exempt_repos_count_toward_group_readiness: bool = True
    # Orchestrator-execution-boundary gate (#115, §17.6 gate 2). When
    # enabled (default), the main agent is blocked from running a HEAVY /
    # long-running foreground Bash command (test suite, build, dev
    # server, long sleep, full-tree sweep); ``run_in_background: true`` is
    # the escape hatch and sub-agents are unrestricted. The one-line
    # kill-switch ``[teatree] orchestrator_bash_gate_enabled = false``
    # disables the gate entirely (also read directly by the hook layer's
    # ``_orchestrator_bash_gate_enabled`` so a `t3 update` that reinstalls
    # the gate stays off until the user flips it back).
    orchestrator_bash_gate_enabled: bool = True
    # Snapshot-baseline pre-commit gate (§17.6). When enabled (default), a
    # commit that stages a Playwright visual baseline (a file under
    # `__snapshots__/` / `<spec>-snapshots/`) is refused unless the ticket
    # carries a green + POSTED `E2eMandatoryRun`. Its OWN kill-switch —
    # `snapshot_baseline_gate_enabled` (per-overlay overridable, DB-first) —
    # disables the hook; the reader (`scripts/hooks/check_snapshot_baseline.py`)
    # resolves it through `get_effective_settings`, so a DB `config_setting set`
    # actuates it exactly like the other gates.
    snapshot_baseline_gate_enabled: bool = True
    # Anti-relaxation + tach-soundness pre-commit gate (§17.6.1/§17.6.2, #850).
    # When enabled (default), the `gate-relaxation` prek hook
    # (`scripts/hooks/check_gate_relaxation.py`) refuses a commit whose staged diff
    # relaxes a lint/coverage constraint or a tach boundary. Its OWN kill-switch —
    # `gate_relaxation_gate_enabled` (per-overlay overridable, DB-first) — disables
    # the hook; the reader resolves it through `get_effective_settings`, so a DB
    # `config_setting set` actuates it exactly like the sibling gates.
    gate_relaxation_gate_enabled: bool = True
    # #122 Safety-biased incremental push gate. SETTLING feature flag: ON (default)
    # => `dev/push-gate.sh` scopes the diff (doctest + ast-grep), FULL on every
    # uncertainty; OFF => whole-tree both sweeps (the pre-#122 behaviour). The
    # default flipped to ON after the CI `selection-audit` soak showed the scoped
    # selection never missed a whole-tree finding. The flag survives as a per-overlay
    # escape hatch; the CI whole-tree backstop is never removed regardless.
    colleague_repo_url_pattern: str = ""
    # Names THIS box in the dashboard header. Empty ships as the default because a
    # machine name cannot be a shipped constant; the header resolves empty to the
    # hostname, so a multi-machine operator can tell two dashboards apart unconfigured.
    dashboard_instance_label: str = ""
    # The header mark, as a STATIC path rather than a filesystem one: a static path is
    # the same string on every box and in every checkout layout, and ``collectstatic``
    # already reaches every installed app's ``static/`` dir on each admin boot — so an
    # overlay ships its own logo beside its code and names it here. Promoted to an
    # overlay code default so that declaration lives in the overlay's repo; a path no
    # static finder resolves renders no mark rather than breaking the header.
    dashboard_logo: str = "dash/logo.jpg"
    solo_repo_url_pattern: str = ""
    # Conventional-Commits title pattern enforced at ``pr create`` BEFORE the
    # gh/glab network call (#1540). A non-matching title is rejected with the
    # pattern printed verbatim; the description is independently required to
    # carry a What/Why header. Per-overlay overridable via
    # ``[overlays.<name>].mr_title_regex = "…"`` so an overlay with a different
    # title grammar declares its own pattern without flipping the global.
    mr_title_regex: str = DEFAULT_MR_TITLE_REGEX


@dataclass
class UserSettings(
    _WorkspaceCoreSettings,
    _ModeHarnessSettings,
    _LoopSettings,
    _OnBehalfSettings,
    _IdentityRoutingSettings,
    _ArchitecturalReviewSettings,
    _ReviewGateSettings,
    _MergeGateSettings,
    _CriticGateSettings,
    _ScannerSettings,
    _ResourcePressureSettings,
    _RetentionSettings,
    _ProvisioningSettings,
    _PrePublishGateSettings,
    _LoopFlagAndCredentialSettings,
    _ArchReviewLoopSettings,
    _BacklogSweepLoopSettings,
    _DirectiveLoopSettings,
    _DogfoodLoopSettings,
    _DreamLoopSettings,
    _HousekeepingLoopSettings,
    _InboxLoopSettings,
    _IssueImplementerLoopSettings,
    _NewsLoopSettings,
    _ResourcePressureLoopSettings,
    _ReviewLoopSettings,
    _SnapshotWarmerLoopSettings,
    _TicketsLoopSettings,
):
    """The ``[teatree]`` settings — the FLAT, 160-field persisted contract.

    The fields are declared across the private in-file group bases above purely for
    readability; ``UserSettings`` is the sole public API and ``dataclasses.fields()``
    stays inheritance-transparent, so the flat field namespace (DB ``ConfigSetting.key``,
    env overrides, cold sqlite3 readers, the rename-guard and golden pin) is unchanged.

    Each base's ``GROUP_PATH`` is where its fields render in the settings hierarchy, and
    this bases tuple is the hierarchy's ORDER — ``teatree.config.setting_groups`` reads
    both off the MRO, so adding a base here is all it takes to add a group.

    CLAUDE.md's "composition over mixins" targets behaviour-carrying classes; these bases
    are pure data-declaration with no behaviour, and the flat schema IS the persisted
    contract, so grouping them as declaration bases (not composed attributes) is the
    deliberate divergence — nesting would be a ~160-key data migration, not a refactor.
    The ``tests/config/test_settings_group_partition.py`` guard pins the group field sets
    pairwise-disjoint and their union == ``dataclasses.fields(UserSettings)`` so a
    silently-shadowed duplicate field can never slip in.
    """


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)
