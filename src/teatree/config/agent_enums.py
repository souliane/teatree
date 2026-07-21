"""Agent-lane config enums — runtime lane, harness transport, provider/credential, eval credential."""

from enum import StrEnum


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
    the credential axis moved to Layer 2. A stored pre-#2887 value is no longer a
    member of this enum; the resolver rejects it loudly rather than silently
    misreading it.

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
        absent, so a typo never silently switches the transport. This is the
        strict BUILT-IN parser (a member of this two-value enum); the OPEN
        ``agent_harness`` setting is parsed by :func:`parse_harness_name`, which
        also admits an overlay-registered harness name.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid agent_harness {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


def parse_harness_name(value: str) -> str:
    """Parse the OPEN ``agent_harness`` setting to a harness registry key (#3157 E1).

    The backend set is no longer closed to the two :class:`AgentHarness` members: an
    overlay registers a third transport under the ``teatree.harnesses`` entry-point group
    (:mod:`teatree.agents.harness_registry`), so this setting must admit any registered
    name, not only the built-in enum. A BUILT-IN name resolves to its :class:`AgentHarness`
    member (a ``StrEnum``, so byte-identical to before for every existing consumer); an
    overlay name returns the plain normalised string. The config layer cannot import the
    agents-layer registry (that would be a backwards dependency edge), so an unregistered
    overlay name is NOT rejected here — it surfaces as a LOUD
    :class:`~teatree.agents.harness_registry.UnknownHarnessError` at dispatch resolution
    rather than a silent wrong transport. An empty value is still rejected here so a blank
    row never resolves to a nameless backend.
    """
    normalised = value.strip().lower()
    if not normalised:
        msg = "Invalid agent_harness: must be a non-empty harness name"
        raise ValueError(msg)
    try:
        return AgentHarness(normalised)
    except ValueError:
        return normalised


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
    | ``pydantic_ai``               | ``anthropic_api``         | ``AnthropicApiKeyCredential``           |

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
        Valid only under ``agent_harness=pydantic_ai`` — the OpenAI-compatible
        router binding, where prompt-cache semantics are opaque.
    *   :attr:`ANTHROPIC_API` — the metered Anthropic API key
        (:class:`~teatree.llm.credentials.AnthropicApiKeyCredential`), driving the
        NATIVE Anthropic Messages-API binding on the ``pydantic_ai`` lane
        ([#3157](https://github.com/souliane/teatree/issues/3157) E1b): a direct
        ``pydantic_ai`` Anthropic model instead of the OpenAI-compatible router
        client, so real ``cache_control`` breakpoints are reachable. Valid only
        under ``agent_harness=pydantic_ai``.

    ``agent_harness_provider`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set agent_harness_provider api_key`` (per-overlay overridable
    with ``--overlay <name>``) or the ``T3_AGENT_HARNESS_PROVIDER`` environment
    variable — a ``[teatree] agent_harness_provider`` TOML value is ignored on
    read.
    """

    SUBSCRIPTION_OAUTH = "subscription_oauth"
    API_KEY = "api_key"
    ORCA_ROUTER_BYOK = "orca_router_byok"
    ANTHROPIC_API = "anthropic_api"

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
    AgentHarness.PYDANTIC_AI: frozenset({AgentHarnessProvider.ORCA_ROUTER_BYOK, AgentHarnessProvider.ANTHROPIC_API}),
}
