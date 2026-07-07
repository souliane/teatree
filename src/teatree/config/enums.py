"""TeaTree config enums — operating mode, throughput dial, autonomy, on-behalf gate, agent runtime."""

from enum import StrEnum


class Mode(StrEnum):
    """Operating mode for agent sessions.

    ``interactive`` (default, conservative on security) gates publishing actions
    on explicit user approval — push, PR creation/merge, external writes all
    stop and ask. ``auto`` grants full autonomy: the agent ships end-to-end
    without confirmation, falling back to interactive only for the non-
    negotiable always-gated list (force-push to default branches, destructive
    shared-state ops). ``mode`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set mode auto`` (per-overlay overridable with ``--overlay
    <name>``) or the ``T3_MODE`` environment variable — a ``[teatree] mode`` TOML
    value is ignored on read.
    """

    INTERACTIVE = "interactive"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str) -> "Mode":
        """Parse a mode string. Invalid values raise ``ValueError``.

        The conservative default (``INTERACTIVE``) is applied by the caller
        when the setting is absent — this function only validates explicit
        values, so typos never silently downgrade to a less-safe mode.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid t3 mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


# Friendly aliases accepted by ``Wip.parse`` and normalised to a canonical
# tier. Module-level (not a class attribute) so ``StrEnum`` does not try to
# treat the mapping as an enum member.
_WIP_ALIASES: dict[str, str] = {
    "low": "slow",
    "normal": "medium",
    "high": "full",
}


class Wip(StrEnum):
    """How much new work a loop tick admits at once — the bounded-WIP dial.

    A single dial spanning sequential to burst throughput. Orthogonal to
    :class:`Mode` and :class:`Autonomy` (which govern *whether* a publishing
    action may proceed); ``wip`` governs *how many* threads of work run
    concurrently — it never relaxes a safety gate.

    Tiers (``SLOW`` < ``MEDIUM`` < ``FULL`` < ``BOOST``, default ``MEDIUM``):

    *   :attr:`SLOW` — at most one implementation worker in flight at a time
        (the cold-review reviewer still runs separately). The cautious dial
        for a fragile tree or a constrained host.
    *   :attr:`MEDIUM` — the conservative baseline: NO orchestrator fan-out.
        Throughput comes only from the intrinsic loop, the PR sweep, and the
        per-overlay ``max_concurrent_auto_starts`` auto-start cap.
    *   :attr:`FULL` — arm ``/loop /t3:wip boost`` so each wave re-classifies
        the backlog and fans out a burst, sustained across waves.
    *   :attr:`BOOST` — a pool-refill burst that keeps ``boost_concurrency
        = N`` live workers in flight, refilling the shortfall each tick;
        clamped to ``max_concurrent_auto_starts``.

    A no-arg ``/t3:wip`` invocation means "go full" regardless of the
    persisted baseline; the persisted value is the resting dial the loop
    reads. ``wip`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set wip full`` (the ``t3 <overlay> wip set <level>``
    wrapper does this), or the ``T3_WIP`` environment variable — a
    ``[teatree] wip`` TOML value is ignored on read.
    """

    SLOW = "slow"
    MEDIUM = "medium"
    FULL = "full"
    BOOST = "boost"

    @classmethod
    def parse(cls, value: str) -> "Wip":
        """Parse a wip string, accepting friendly aliases; typos raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default (:attr:`MEDIUM`)
        is applied by the caller when the setting is absent, so this validates
        only explicit values and a typo never silently changes throughput.
        ``low``/``normal``/``high`` map onto ``slow``/``medium``/``full``.
        """
        normalised = value.strip().lower()
        normalised = _WIP_ALIASES.get(normalised, normalised)
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            aliases = ", ".join(sorted(_WIP_ALIASES))
            msg = f"Invalid wip {value!r}; valid values: {valid} (aliases: {aliases})"
            raise ValueError(msg) from exc


class Autonomy(StrEnum):
    """The single per-overlay trust switch collapsing the three user-approval gates.

    Tiers (``FULL`` > ``NOTIFY`` > ``BABYSIT``, default ``BABYSIT``):

    *   :attr:`BABYSIT` — every approval gate keeps its own value; the user
        stays in the loop on merges, answers, and colleague-visible posts.
        Review-request posting follows ``on_behalf_post_mode`` like any other
        colleague-visible post.
    *   :attr:`NOTIFY` — autonomous, but every on-behalf action DMs the user
        (derived ``notify_on_behalf``) and the user's MR merges only after a
        colleague approval (per-diff CLEAR, never self-approve). The
        collaborative/customer surface: the resolved ``review_request_post_disabled``
        is ``True`` so the agent never auto-requests review — it stops at "MR is
        mergeable + review-requestable".
    *   :attr:`FULL` — autonomous with no after-the-fact DM; the single-author
        ``solo_overlay`` merge bypass is reachable here only, and the substrate
        per-PR sign-off is satisfied by this standing grant (the §17.4.3 step 5
        carve-out — see :func:`teatree.core.merge.execution.assert_merge_preconditions`)
        so a substrate CLEAR needs no per-CLEAR ``human_authorizer``. The solo
        tooling surface: the resolved ``review_request_post_disabled`` is ``False``
        so review-request proceeds.

    Both autonomous tiers collapse the three gates, pin ``mode = auto``, and set
    the resolved ``review_request_post_disabled`` off the tier (#2579, replacing
    the deleted ``agent_review_request_disabled`` side flag) — see
    :func:`_apply_autonomy`. An explicit per-gate value always wins. The
    safety floor (privacy/leak gate, cold-review with reviewer != maker,
    CI-green, not-draft, never-lockout, the SHA-bound audited keystone
    transition) is out of scope and never touched — under ``full`` the substrate
    carve-out removes ONLY the per-PR human sign-off, never a floor guard.
    """

    BABYSIT = "babysit"
    NOTIFY = "notify"
    FULL = "full"

    @classmethod
    def parse(cls, value: str) -> "Autonomy":
        """Parse an autonomy string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`BABYSIT`) is applied by the caller when the setting is
        absent, so a typo never silently grants full autonomy.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid autonomy {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class TeamsDisplay(StrEnum):
    """How a Track-B maker pane is DISPLAYED — presentation-only (WI-5, #1838).

    A maker pane's SDK session always runs in-process (the source of truth); this
    setting governs only whether that same session is ALSO rendered in a visible
    terminal pane. The mechanism is tmux control mode (``tmux -CC``): under it
    iTerm2 renders ``tmux split-window`` panes as native split panes in one tab;
    elsewhere it degrades to plain tmux panes. The naming mirrors Claude Code's
    own ``teammateMode`` (``tmux`` / ``in-process``, with ``auto`` probing).

    Tiers (default :attr:`NONE`, ships dark):

    *   :attr:`NONE` (default) — no display layer. The in-process SDK path is
        unchanged; behaviour is byte-identical to today. The conservative default.
    *   :attr:`AUTO` — display via tmux WHEN a multiplexer is detected (``$TMUX``
        set, a ``tmux`` binary, a TTY), else fall back to the in-process path. The
        always-degrades-gracefully tier.
    *   :attr:`TMUX` — prefer the tmux display; still falls back to in-process when
        no tmux / no TTY / a spawn failure (the display never replaces the SDK run).

    Read from ``[teams] display`` (the feature namespace, alongside ``enabled``);
    per-overlay overridable via ``[overlays.<name>].teams_display``;
    ``T3_TEAMS_DISPLAY`` env wins. A typo / bad value fails SAFE to :attr:`NONE`
    — the presentation layer can never escalate itself on by a mistyped value.
    """

    NONE = "none"
    AUTO = "auto"
    TMUX = "tmux"

    @classmethod
    def parse(cls, value: str) -> "TeamsDisplay":
        """Parse a teams-display string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default (:attr:`NONE`) is
        applied by the caller when the setting is absent, so this validates only
        explicit values and a typo in a TOML/DB tier is rejected LOUD. The env
        tier instead fails SAFE to :attr:`NONE` (see ``_parse_env_teams_display``)
        so a mistyped env var never crashes the resolver.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid teams display {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class OnBehalfPostMode(StrEnum):
    """Tri-state pre-gate over on-behalf colleague-VISIBLE posts (#960).

    Three points on the autonomy ramp for colleague-visible posts the
    agent makes *as the user* to a colleague/customer surface (PR/MR
    comment, issue comment, Slack channel/thread post, Notion post, PR/MR
    approve, reaction on someone else's message).

    Colleague-INVISIBLE *draft* notes (``t3 review post-draft-note``) are
    exempt from this gate under EVERY mode — a draft is never visible to
    colleagues (only the user can submit it), so it never needs approval.
    That exemption is the whole purpose of the setting: keep the user in
    control of their colleague-visible voice while letting the agent draft
    freely. Drafts always publish autonomously; under :attr:`ASK` /
    :attr:`DRAFT_OR_ASK` the agent additionally DMs the user with the
    publish/delete commands so they can review and submit.

    *   :attr:`DRAFT_OR_ASK` (default) and :attr:`ASK` behave identically:
        both auto-publish a draft (+ DM the user) and both BLOCK every
        colleague-visible post until the user records an approval. They
        are kept as distinct names for clarity and backward compatibility;
        the per-action draft exemption is what makes a draft ungated, not
        the mode.
    *   :attr:`ASK` — every colleague-VISIBLE action requires an explicit
        recorded approval (``t3 review approve-on-behalf <target> <action>
        --approver <id>``) before it publishes. Drafts are exempt.
    *   :attr:`IMMEDIATE` — the gate is off; gated actions publish
        directly (subject to the always-gated list in :class:`Mode`).

    The user satisfies the gate for a colleague-visible post without a TTY
    by recording an
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`;
    DMs *to the user themselves*, draft notes, and internal-only
    orchestration writes are out of scope and remain ungated under every
    mode.
    """

    DRAFT_OR_ASK = "draft_or_ask"
    ASK = "ask"
    IMMEDIATE = "immediate"

    @classmethod
    def parse(cls, value: str) -> "OnBehalfPostMode":
        """Parse an on-behalf-post-mode string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`DRAFT_OR_ASK`) is applied by the caller when the setting
        is absent, so typos never silently downgrade to a less-safe
        mode.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid on_behalf_post_mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class CriticGateMode(StrEnum):
    """Tri-state enforcement posture for the user-proxy critic gate (SELFCATCH-5).

    Re-typed from the former boolean enforcement flag (#104), which coupled
    "arm the expensive async LLM critic" with "block on a finding", leaving no
    way to accumulate ``CriticVerdict`` evidence without also blocking. The three
    points on the ramp:

    *   :attr:`OFF` (default) — dark: the cheap deterministic findings are still
        recorded (advisory evidence), but the EXPENSIVE async LLM critic is never
        armed and no finding blocks — a customer overlay that never opts in
        creates no ``Session``/``Task``/``CriticDispatch``.
    *   :attr:`ADVISORY` — armed: the async critic dispatches and records
        ``CriticVerdict``/``CriticFinding`` rows, but a blocking finding never
        raises. The mode that accumulates critic-liveness evidence pre-enablement.
    *   :attr:`BLOCKING` — armed and enforcing: a blocking deterministic finding
        refuses the delivery (``CriticGateError``), the ticket stays RETROSPECTED.
    """

    OFF = "off"
    ADVISORY = "advisory"
    BLOCKING = "blocking"

    @classmethod
    def parse(cls, value: str) -> "CriticGateMode":
        """Parse a critic-gate-mode string; invalid values raise ``ValueError``.

        The conservative default (:attr:`OFF`) is applied by the caller when the
        setting is absent, so a typo never silently arms or un-blocks the critic.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid critic_gate_mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class SendProxyMode(StrEnum):
    """Enforcement posture for the outbound send-proxy destination allowlist (#117).

    Every outbound artifact (Slack post/DM/react, forge PR/MR/issue comment)
    routes through :mod:`teatree.core.send_proxy`, which redaction-scans the
    payload and checks the destination against the per-overlay allowlist. This
    mode decides what the proxy DOES with a non-allowlisted destination or a
    redaction hit:

    *   :attr:`WARN` (default) — audit-only: every send is recorded in a
        :class:`~teatree.core.models.send_audit.SendAudit` row with the
        would-be allowlist verdict and redaction matches, but the send is
        NEVER blocked and the live payload is NEVER mutated. This is the safe
        ship posture: it accumulates the destination soak an operator seeds the
        allowlist from before ever flipping to :attr:`ENFORCE`.
    *   :attr:`ENFORCE` — deterministic block: a destination absent from the
        per-overlay allowlist is refused and the payload is redacted before the
        wire call. Only turned on after the allowlist is seeded from a WARN-mode
        soak (else it would over-block legitimate posts).

    The user's own DM destination is always allowed under BOTH modes (the
    never-lockout carve-out) so the bot→user notify path can never be gated by
    a mis-seeded allowlist.
    """

    WARN = "warn"
    ENFORCE = "enforce"

    @classmethod
    def parse(cls, value: str) -> "SendProxyMode":
        """Parse a send-proxy-mode string; invalid values raise ``ValueError``.

        The conservative default (:attr:`WARN`, audit-only) is applied by the
        caller when the setting is absent, so a typo never silently arms
        enforcement.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid send_proxy_mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class MissingIssuePolicy(StrEnum):
    """What to do when a commit/MR needs an issue reference and none is in hand.

    The recurring failure this setting encodes: the agent is about to make a
    commit or open an MR/PR that links to an issue/ticket, but it has no
    reference. The correct behaviour is NOT to improvise — it is to first
    recover the ORIGINAL existing issue (the one that introduced the bug or
    left the scope unimplemented, found by searching the repo's issues and the
    introducing commit's linked issue) and use THAT. Only when no existing
    issue is found does the policy decide the fallback, and the fallback differs
    by repo class:

    *   On a **colleague-facing / external** repo (a shared product repo of an
        org, not one the user owns), the agent must NEVER auto-create an issue
        and never use a placeholder/dummy reference — it must ASK the user. A
        fabricated issue or a dummy ref on a colleague-facing repo pollutes a
        shared tracker the user does not control.
    *   On the **user's own** repo (teatree itself, the user's solo overlay
        repos), creating an issue is allowed without asking — the user owns the
        tracker, so a created issue is self-bookkeeping, not noise on a
        colleague's surface.

    Tiers (default :attr:`FIND_EXISTING_THEN_ASK`, the conservative choice):

    *   :attr:`FIND_EXISTING_THEN_ASK` (default) — search for the original
        existing issue first; if none is found, ASK on a colleague-facing repo
        and CREATE on the user's own repo. Never a dummy ref. This is the
        never-improvise default.
    *   :attr:`CREATE` — opt-in. After the existing-issue search comes up empty,
        the agent is authorised to auto-create an issue even on a
        colleague-facing repo. The user has accepted the agent filing issues on
        the shared tracker.
    *   :attr:`DUMMY` — opt-in. After the existing-issue search comes up empty,
        the agent is authorised to use a placeholder/dummy reference even on a
        colleague-facing repo. The most permissive tier — the user accepts a
        non-real ref rather than a stop-and-ask.

    ``create`` and ``dummy`` are opt-in only; the default is OFF for
    colleague-facing repos (it never auto-creates and never uses a dummy there).
    ``missing_issue_ref_policy`` is a DB-home setting: read from the
    ``ConfigSetting`` store (``t3 <overlay> config_setting set
    missing_issue_ref_policy <value>``), per-overlay overridable with
    ``--overlay <name>``; the ``T3_MISSING_ISSUE_POLICY`` env var wins over
    both. A ``[teatree]`` / ``[overlays.<name>]`` TOML value is ignored on
    read. Resolved by
    :func:`teatree.missing_issue_policy.resolve_missing_issue_verdict`, and the
    agent-facing prose lives in ``skills/ship/SKILL.md`` § "Missing Issue
    Reference Policy".
    """

    FIND_EXISTING_THEN_ASK = "find_existing_then_ask"
    CREATE = "create"
    DUMMY = "dummy"

    @classmethod
    def parse(cls, value: str) -> "MissingIssuePolicy":
        """Parse a missing-issue-policy string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`FIND_EXISTING_THEN_ASK`) is applied by the caller when the
        setting is absent, so a typo never silently opts the agent into
        auto-creating or dummy-referencing on a colleague-facing repo.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid missing_issue_ref_policy {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class AgentRuntime(StrEnum):
    """WHICH LANE a loop-dispatched phase agent executes in — interactive vs headless.

    A loop-dispatched phase task is one whose ``(role, phase)`` has a registered
    phase sub-agent (``t3:coder`` / ``t3:reviewer`` / …, see
    ``SUBAGENT_BY_PHASE``). This setting decides ONLY the lane such a task runs
    in. WHICH in-process transport a headless run rides, and WHICH
    provider/credential that transport authenticates with, is the orthogonal
    two-layer pair :class:`AgentHarness` (Layer 1) / :class:`AgentHarnessProvider`
    (Layer 2) — [#2887](https://github.com/souliane/teatree/issues/2887) retired
    the credential distinction this enum used to carry itself (the former
    ``sdk_oauth`` / ``sdk_apikey`` / ``api`` members): conflating "which lane"
    with "which credential" in one enum meant the two could never be set
    independently, and the not-yet-implemented ``api`` member had no home once
    the credential axis moved to Layer 2. A stored pre-#2887 value is collapsed
    onto this shape by migration ``0015_agent_harness_two_layer_config``.

    Tiers (default :attr:`INTERACTIVE`, today's behaviour):

    *   :attr:`INTERACTIVE` (default) — the in-session ``/loop`` slot claims the
        task (``loop_dispatch claim-next``) and spawns the phase sub-agent via the
        ``Agent`` tool, in the live Claude Code session (subscription-covered,
        visible in the agent view). No behaviour change from before this setting.
    *   :attr:`HEADLESS` — ``run_headless`` (``agents/headless.py``) drives an
        in-process agent session behind the :class:`AgentHarness` transport seam.
        The transport (``claude_sdk`` default | ``pydantic_ai``) is
        :class:`AgentHarness`'s call; for ``claude_sdk`` the Anthropic credential
        (subscription OAuth default | metered API key) is
        :class:`AgentHarnessProvider`'s call.

    ``agent_runtime`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set agent_runtime headless`` (per-overlay overridable with
    ``--overlay <name>``) or the ``T3_AGENT_RUNTIME`` environment variable — a
    ``[teatree] agent_runtime`` TOML value is ignored on read.
    """

    INTERACTIVE = "interactive"
    HEADLESS = "headless"

    @classmethod
    def parse(cls, value: str) -> "AgentRuntime":
        """Parse an agent-runtime string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`INTERACTIVE`) is applied by the caller when the setting is
        absent, so a typo never silently switches the runtime.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid agent_runtime {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc

    @property
    def is_headless(self) -> bool:
        """True for :attr:`HEADLESS` — the sole non-interactive lane."""
        return self is AgentRuntime.HEADLESS


class AgentHarness(StrEnum):
    """Which in-process TRANSPORT drives a headless agent run — the harness backend.

    Orthogonal to :class:`AgentRuntime`, which selects the interactive-vs-headless
    lane and its credential: once a run IS headless, ``agent_harness`` picks the
    in-process transport that opens the agent session behind the narrow
    ``teatree.agents.harness.Harness`` protocol. Transport is not the same axis as
    interactive/headless, so it is its own setting rather than a fold into
    :class:`AgentRuntime`.

    Tiers (default :attr:`CLAUDE_SDK`, today's behaviour):

    *   :attr:`CLAUDE_SDK` (default) — the ``claude-agent-sdk`` transport
        (:class:`~teatree.agents.harness.ClaudeSdkHarness`, wrapping
        ``ClaudeSDKClient``). Byte-identical to the transport before the seam.
    *   :attr:`PYDANTIC_AI` — the provider-agnostic transport
        (:class:`~teatree.agents.harness.PydanticAiHarness`,
        [#2885](https://github.com/souliane/teatree/issues/2885)): a
        ``pydantic_ai.Agent`` targeting OrcaRouter's BYOK, OpenAI-compatible,
        metered endpoint. Its OrcaRouter credential resolves lazily inside
        ``open``, so selecting this value never itself requires a live
        credential.

    ``agent_harness`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set agent_harness pydantic_ai`` (per-overlay overridable with
    ``--overlay <name>``) or the ``T3_AGENT_HARNESS`` environment variable — a
    ``[teatree] agent_harness`` TOML value is ignored on read.
    """

    CLAUDE_SDK = "claude_sdk"
    PYDANTIC_AI = "pydantic_ai"

    @classmethod
    def parse(cls, value: str) -> "AgentHarness":
        """Parse an agent-harness string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`CLAUDE_SDK`) is applied by the caller when the setting is
        absent, so a typo never silently switches the transport.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid agent_harness {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class AgentHarnessProvider(StrEnum):
    """Layer 2 of the two-layer harness config model — the provider/credential.

    [#2887](https://github.com/souliane/teatree/issues/2887): the "single home"
    for the provider/credential a headless run authenticates with, CONSTRAINED by
    Layer 1 (:class:`AgentHarness`) via :meth:`valid_for`. Mirrors the resolution
    table :class:`~teatree.llm.credentials.OrcaRouterCredential` already
    documents in prose:

    | Layer 1 (``agent_harness``) | Layer 2 (this enum)     | Credential                            |
    |------------------------------|---------------------------|-----------------------------------------|
    | ``claude_sdk``                | ``subscription_oauth``    | ``AnthropicSubscriptionCredential``     |
    | ``claude_sdk``                | ``api_key``               | ``AnthropicApiKeyCredential``           |
    | ``pydantic_ai``               | ``orca_router_byok``      | ``OrcaRouterCredential``                |

    A Vertex AI Layer-2 provider under ``pydantic_ai`` is reserved but not yet
    implemented (see ``OrcaRouterCredential``'s docstring), so it carries no enum
    member yet — :meth:`valid_for` names only what is actually selectable today.

    Tiers (default :attr:`SUBSCRIPTION_OAUTH`, today's ``claude_sdk`` behaviour):

    *   :attr:`SUBSCRIPTION_OAUTH` (default) — the plan's OAuth token
        (:class:`~teatree.llm.credentials.AnthropicSubscriptionCredential`).
        Valid only under ``agent_harness=claude_sdk``.
    *   :attr:`API_KEY` — the metered Anthropic API key
        (:class:`~teatree.llm.credentials.AnthropicApiKeyCredential`).
        Valid only under ``agent_harness=claude_sdk``.
    *   :attr:`ORCA_ROUTER_BYOK` — OrcaRouter's BYOK metered key
        (:class:`~teatree.llm.credentials.OrcaRouterCredential`).
        Valid only under ``agent_harness=pydantic_ai`` — it is the ONLY Layer-2
        provider a ``pydantic_ai`` run has today, so
        :class:`~teatree.agents.harness.PydanticAiHarness` does not yet branch on
        this setting (there is nothing else to pick); it ships wired for the
        constraint table and a future Vertex binding, not yet as an active
        branch.

    ``agent_harness_provider`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set agent_harness_provider api_key`` (per-overlay overridable
    with ``--overlay <name>``) or the ``T3_AGENT_HARNESS_PROVIDER`` environment
    variable — a ``[teatree] agent_harness_provider`` TOML value is ignored on
    read.
    """

    SUBSCRIPTION_OAUTH = "subscription_oauth"
    API_KEY = "api_key"
    ORCA_ROUTER_BYOK = "orca_router_byok"

    @classmethod
    def parse(cls, value: str) -> "AgentHarnessProvider":
        """Parse an agent-harness-provider string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`SUBSCRIPTION_OAUTH`) is applied by the caller when the setting is
        absent, so a typo never silently switches the credential.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid agent_harness_provider {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc

    @classmethod
    def valid_for(cls, harness: "AgentHarness") -> frozenset["AgentHarnessProvider"]:
        """The Layer-2 providers CONSTRAINED-VALID under Layer-1 *harness*."""
        return _VALID_PROVIDERS_BY_HARNESS[harness]


_VALID_PROVIDERS_BY_HARNESS: dict[AgentHarness, frozenset[AgentHarnessProvider]] = {
    AgentHarness.CLAUDE_SDK: frozenset({AgentHarnessProvider.SUBSCRIPTION_OAUTH, AgentHarnessProvider.API_KEY}),
    AgentHarness.PYDANTIC_AI: frozenset({AgentHarnessProvider.ORCA_ROUTER_BYOK}),
}


class EvalCredential(StrEnum):
    """Which Anthropic credential the automated eval lane authenticates with.

    The single knob that reverses [#2707](https://github.com/souliane/teatree/issues/2707)'s
    metered-exclusive lock. It selects the credential KIND the metered ``api``
    eval backend (and the LLM judge) rides — mirroring :class:`AgentRuntime`, the
    two are mutually exclusive so a single enum (not two flags) is the honest shape:

    *   :attr:`SUBSCRIPTION_OAUTH` (default) — the plan's OAuth token
        (:class:`~teatree.llm.credentials.AnthropicSubscriptionCredential`,
        ``CLAUDE_CODE_OAUTH_TOKEN``). Draws no per-token bill. Its cost is the
        subscription's depleting 5h/7d usage window, so the automated lane MUST be
        RIGHT-SIZED (a single effort tier, a smaller trial count, per-account
        routing via ``anthropic_oauth_pass_paths``) or a full fan-out throttles the
        window mid-run AND starves the main loop (same token).
    *   :attr:`METERED_API_KEY` — the metered key
        (:class:`~teatree.llm.credentials.AnthropicApiKeyCredential`,
        ``ANTHROPIC_API_KEY``). Billed per token; no subscription draw, no usage
        window. The pre-#2707-reversal default; still selectable via this knob.

    ``eval_credential`` is a DB-home setting: set it via ``t3 <overlay>
    config_setting set eval_credential subscription_oauth`` (per-overlay overridable
    with ``--overlay <name>``) or the ``T3_EVAL_CREDENTIAL`` environment variable — a
    ``[teatree] eval_credential`` TOML value is ignored on read.
    """

    SUBSCRIPTION_OAUTH = "subscription_oauth"
    METERED_API_KEY = "metered_api_key"

    @classmethod
    def parse(cls, value: str) -> "EvalCredential":
        """Parse an eval-credential string; invalid values raise ``ValueError``.

        Mirrors :meth:`AgentRuntime.parse`: the default (:attr:`SUBSCRIPTION_OAUTH`)
        is applied by the caller when the setting is absent, so a typo never
        silently switches the eval lane onto the metered API key (real cost).
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid eval_credential {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc
