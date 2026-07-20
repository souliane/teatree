r"""Provider-neutral credential resolution for Claude SDK / bundled-CLI invocations.

THE canonical way to authenticate any Claude SDK or bundled-``claude``-CLI
invocation in teatree. A :class:`Credential` resolves a single secret from an
ordered list of injected :class:`CredentialSource`\ s (env wins, then the
``pass`` store), fails loud with a :class:`CredentialError` naming the exact fix
when neither yields a value, and builds a child process env that sets its own env
var while **stripping** every conflicting credential — so a metered invocation can
never silently fall back to a different credential.

The two concrete rules are mirror images. :class:`AnthropicApiKeyCredential` sets
``ANTHROPIC_API_KEY`` and strips the subscription ``CLAUDE_CODE_OAUTH_TOKEN``;
:class:`AnthropicSubscriptionCredential` sets ``CLAUDE_CODE_OAUTH_TOKEN`` and strips
``ANTHROPIC_API_KEY`` (the bundled CLI prefers a credential only when the others are
absent). The subscription rule additionally FORBIDS ``ANTHROPIC_BASE_URL``
(:data:`ANTHROPIC_BASE_URL_ENV`) rather than stripping it: plan auth is valid only
against Anthropic's own endpoint, so a base-URL redirect alongside it is a
misconfiguration to surface, not a fallback to remove — see ``CredentialSpec``'s
``forbidden_vars``. NO credential carries a built-in default ``pass`` path — each resolves only
from its env var or an explicitly configured per-account entry, and fails loud (naming
the setting to configure) when neither is present rather than reading a dead default.
Which one the automated eval lane rides is the ``eval_credential`` knob's
call, resolved through ``teatree.credential_config.resolve_eval_credential``: the
default is the subscription credential, reversing [#2707](https://github.com/souliane/teatree/issues/2707)'s
metered-exclusive lock (the metered key is still selectable via the knob). The
subscription credential also backs the non-eval Claude invocations (the headless
loop). This module stays foundation-pure — it enforces "use THIS credential,
exclusively", not which one the eval lane picks.

The pieces are named provider-agnostically (``Credential`` / ``CredentialSpec`` /
``CredentialSource``) so this layer later becomes an ``LLMBackend.credential`` —
Claude today, other providers (OpenRouter, …) later — with no rework at the call
sites. Dependency-injected sources keep the whole surface unit-testable without
touching the real environment or the ``pass`` store. :class:`OrcaRouterCredential`
is the first non-Anthropic tenant of this pattern: the
``pydantic_ai`` headless harness's ([#2885](https://github.com/souliane/teatree/issues/2885))
BYOK, OpenAI-compatible provider, resolved the identical env-then-``pass`` way but
carrying no conflicting Anthropic vars (an orthogonal provider, not a mirror-image
rule).

A credential's ``pass_path`` MAY be overridden per instance by an INJECTED
``pass_path_override`` (a plain string) — the resolved per-account ``pass`` entry
an operator routes to. This module stays foundation-pure: it never reads the
config store itself. The DB-config read (``ConfigSetting``, the per-overlay routing
lists ``anthropic_oauth_pass_paths`` / ``anthropic_api_key_pass_paths``, overlay
scope then global) lives in the domain-layer factory ``teatree.credential_config``,
whose selector picks a healthy account and injects its ``pass`` path here. ``spec``
stays a static constant the consumers (``eval/isolation.py``'s default strip set,
the eval chokepoints' ``spec.conflicting_vars`` / ``spec.env_var`` reads) read
safely; only ``_effective_spec`` applies the injected override at resolve time.
"""

import dataclasses
import os
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from teatree.utils.secrets import read_pass


class CredentialError(RuntimeError):
    """Raised when a :class:`Credential` can resolve no value from any source.

    The message names the exact fix — set the env var or ``pass insert <path>`` —
    and states that this credential never falls back to a conflicting one, so the
    user is never left guessing why the run refused.
    """


@dataclasses.dataclass(frozen=True)
class CredentialSpec:
    """The identity of one credential: its env var, ``pass`` path, and conflicts.

    *conflicting_vars* are the credentials that must be REMOVED from a child env
    when this one is applied — the bundled CLI prefers one credential only when
    the others are absent, so stripping the conflicts is what makes "use THIS
    credential, exclusively" actually hold.

    *forbidden_vars* are environment variables that must NOT be present when this
    credential is applied — :meth:`Credential.child_env` RAISES on them rather than
    stripping them. The distinction from *conflicting_vars* is deliberate and is the
    whole point of the separate field: a conflicting credential is silently removed
    (the child simply must not fall back to it), whereas a forbidden var names an
    operator misconfiguration that must be surfaced, not papered over. The concrete
    case is ``ANTHROPIC_BASE_URL`` on the subscription credential — redirecting a
    plan-authenticated ``claude`` child at a third-party endpoint is never what the
    operator meant, and silently dropping the redirect would leave them believing a
    gateway was in use. It stays legal on the API-key credential, which is why it is
    not a conflict of the pair.

    *pass_path* is ``None`` for a credential that has NO built-in default entry:
    it then resolves only from *env_var* or an INJECTED per-account
    ``pass_path_override``, and :meth:`Credential.resolve` fails loud when neither
    is present rather than reading a dead default. *routing_setting* names the
    config setting an operator populates to supply that override (e.g.
    ``anthropic_oauth_pass_paths``), so the loud error is actionable.
    """

    env_var: str
    conflicting_vars: tuple[str, ...]
    pass_path: str | None = None
    routing_setting: str | None = None
    forbidden_vars: tuple[str, ...] = ()


@runtime_checkable
class CredentialSource(Protocol):
    """A place a credential value can be read from.

    :meth:`lookup` is handed the WHOLE :class:`CredentialSpec` and reads the field
    it understands — :class:`EnvSource` reads ``spec.env_var`` from the
    environment, :class:`PassSource` reads ``spec.pass_path`` from the store. The
    spec carries both keys, so :class:`Credential` stays agnostic about which key
    a given source consults.
    """

    def lookup(self, spec: "CredentialSpec") -> str | None: ...


class EnvSource:
    """Reads a credential from the process environment (``os.environ``)."""

    def lookup(self, spec: "CredentialSpec") -> str | None:  # noqa: PLR6301 — instance method to satisfy the CredentialSource Protocol.
        return os.environ.get(spec.env_var) or None


class PassSource:
    """Reads a credential from the ``pass`` password store under ``spec.pass_path``.

    A ``None`` ``pass_path`` (a credential with no built-in default entry and no
    injected override) is skipped entirely — nothing to read. A missing entry (or
    ``pass`` not installed) resolves to ``None`` — handled as absent, never a crash.
    :func:`~teatree.utils.secrets.read_pass` already swallows the non-zero ``pass
    show`` exit and returns ``""`` for an absent entry, which this normalizes to ``None``.
    """

    def lookup(self, spec: "CredentialSpec") -> str | None:  # noqa: PLR6301 — instance method to satisfy the CredentialSource Protocol.
        if spec.pass_path is None:
            return None
        return read_pass(spec.pass_path) or None


_DEFAULT_SOURCES: tuple[CredentialSource, ...] = (EnvSource(), PassSource())


class Credential:
    """Resolve one credential (env wins, then ``pass``) and build a clean child env.

    Subclasses supply :attr:`spec`. *sources* are injected (default: env then
    ``pass``) so the whole behaviour is unit-testable with fakes — no real
    environment or ``pass`` store needed. *pass_path_override*, when given, is the
    per-account ``pass`` entry that replaces :attr:`spec`'s built-in ``pass_path``
    at RESOLVE time; it is a plain string injected by the domain-layer factory
    (``teatree.credential_config``), so this foundation module never reads the
    config store itself. ``None`` (the default) leaves the built-in ``pass_path``.
    *missing_context*, when given, is an opaque note the domain layer appends to the
    loud :class:`CredentialError` (e.g. which config scope had no configured account) —
    rendered only on failure, so a caller that never resolves is never affected.
    """

    spec: CredentialSpec

    def __init__(
        self,
        *,
        sources: Sequence[CredentialSource] = _DEFAULT_SOURCES,
        pass_path_override: str | None = None,
        missing_context: str | None = None,
    ) -> None:
        self._sources = tuple(sources)
        self._pass_path_override = pass_path_override
        self._missing_context = missing_context

    @property
    def sources(self) -> tuple[CredentialSource, ...]:
        """The ordered credential sources consulted by :meth:`resolve`."""
        return self._sources

    def resolve(self) -> str:
        """Return the credential value from the first source that yields one.

        Sources are consulted in order (env wins, then ``pass``) against the
        EFFECTIVE spec (``spec`` with any injected pass_path override applied);
        the first non-empty value wins and later sources are not
        consulted. When none yields a value, raise :class:`CredentialError`
        naming the fix — never a silent empty string that would let a metered
        invocation authenticate as nothing or fall back to a different credential.
        """
        spec = self._effective_spec()
        for source in self._sources:
            value = source.lookup(spec)
            if value:
                return value
        raise CredentialError(self._missing_message(spec, self._missing_context))

    def _effective_spec(self) -> CredentialSpec:
        """:attr:`spec` with any injected ``pass_path`` override applied.

        Only ``pass_path`` is overridable — ``env_var`` and ``conflicting_vars``
        are the credential's fixed identity, so :meth:`export` / :meth:`child_env`
        keep reading them off the static :attr:`spec`. With no injected override the
        static ``spec`` stands unchanged.
        """
        if self._pass_path_override:
            return dataclasses.replace(self.spec, pass_path=self._pass_path_override)
        return self.spec

    def export(self) -> str:
        """Resolve the credential, write it into ``os.environ``, and return it.

        The in-process side effect a downstream docker ``-e VARNAME`` pass-through
        relies on: forwarding ``-e ANTHROPIC_API_KEY`` only carries the value when
        it is present in the current process environment. Raises
        :class:`CredentialError` when no value is resolvable, so a metered
        invocation fails loud rather than exporting nothing. (Conflict stripping
        is :meth:`child_env`'s job — the spawned child's env, not this process's.)
        """
        value = self.resolve()
        os.environ[self.spec.env_var] = value
        return value

    def child_env(self, base: Mapping[str, str]) -> dict[str, str]:
        """Return a copy of *base* with this credential set and its conflicts stripped.

        The resolved value is written under :attr:`spec`'s ``env_var`` and every
        ``conflicting_vars`` entry is REMOVED, so the spawned SDK / CLI child
        authenticates with exactly this credential and cannot fall back to a
        conflicting one. *base* is never mutated.

        A ``forbidden_vars`` entry present in *base* RAISES :class:`CredentialError`
        before anything is resolved — a forbidden var is an operator misconfiguration
        this credential refuses to run under, not a fallback to quietly remove. Raises
        :class:`CredentialError` too when no value is resolvable (the loud refusal
        propagates to the caller).
        """
        self._reject_forbidden(base)
        value = self.resolve()
        child = dict(base)
        for conflicting in self.spec.conflicting_vars:
            child.pop(conflicting, None)
        child[self.spec.env_var] = value
        return child

    def _reject_forbidden(self, base: Mapping[str, str]) -> None:
        """Raise when *base* carries a var this credential refuses to run alongside.

        Checked BEFORE :meth:`resolve` so the refusal names the misconfiguration
        rather than a downstream missing-token error. Empty values are treated as
        absent — an exported-but-blank var expresses no redirect.
        """
        present = [var for var in self.spec.forbidden_vars if base.get(var, "").strip()]
        if not present:
            return
        names = ", ".join(present)
        msg = (
            f"{names} is set, but {self.spec.env_var} authenticates against the Anthropic "
            f"subscription, which is only valid against Anthropic's own endpoint. Redirecting a "
            f"plan-authenticated child at another endpoint is refused. Either unset {names}, or "
            f"pin agent_harness_provider=api_key to route a metered key through that endpoint."
        )
        raise CredentialError(msg)

    @staticmethod
    def _missing_message(spec: CredentialSpec, context: str | None = None) -> str:
        note = f" {context}" if context else ""
        if spec.pass_path is None:
            setting = spec.routing_setting or "the per-account routing list"
            return (
                f"no {spec.env_var} credential available{note} and no OAuth `pass` path is configured. "
                f"Set {spec.env_var} in the environment, or configure a per-account `pass` "
                f"entry via the `{setting}` setting so the routing selector resolves one. "
                "This credential has no default `pass` path — it never falls back to a dead "
                f"entry, and its conflicting vars {spec.conflicting_vars} are stripped from the "
                "child env, so a misconfigured run fails loud here rather than authenticating as the wrong one."
            )
        return (
            f"no {spec.env_var} credential available{note}. Set {spec.env_var} in the "
            f"environment, or store it locally with `pass insert {spec.pass_path}`. "
            "This credential never falls back to a conflicting one (the conflicting "
            f"vars {spec.conflicting_vars} are stripped from the child env), so a "
            "misconfigured run fails loud here rather than authenticating as the wrong one."
        )


#: The Anthropic SDK / ``claude`` CLI base-URL override. Both read it natively, and a
#: spawned CLI child inherits it from the ambient env, so it silently redirects every
#: request the child makes. Legal on the metered API key (a gateway, Bedrock/Vertex, or
#: an Anthropic-compatible third-party provider on ITS OWN key); forbidden alongside the
#: subscription token, whose plan auth is only valid against Anthropic's own endpoint.
ANTHROPIC_BASE_URL_ENV = "ANTHROPIC_BASE_URL"


class AnthropicApiKeyCredential(Credential):
    """The metered Anthropic API key — strips the subscription OAuth token.

    The metered credential the eval lane rides when ``eval_credential`` is set to
    ``metered_api_key`` (per-token cost, no usage window). Its child env sets
    ``ANTHROPIC_API_KEY`` and removes ``CLAUDE_CODE_OAUTH_TOKEN`` so the SDK /
    bundled CLI authenticates with exactly this key. It has NO built-in default
    ``pass`` path: it resolves only from its env var or an injected per-account
    ``pass_path_override`` — selected from the ``anthropic_api_key_pass_paths`` routing
    list by ``teatree.credential_config.resolve_api_key_credential`` — and
    :meth:`resolve` fails loud (naming ``anthropic_api_key_pass_paths``) when neither
    is present rather than reading a dead built-in entry.
    """

    spec = CredentialSpec(
        env_var="ANTHROPIC_API_KEY",
        conflicting_vars=("CLAUDE_CODE_OAUTH_TOKEN",),
        pass_path=None,
        routing_setting="anthropic_api_key_pass_paths",
    )


class AnthropicSubscriptionCredential(Credential):
    """The subscription OAuth token — strips the metered API key.

    The plan's credential: the DEFAULT the eval lane rides (reversing #2707) AND the
    credential the non-eval Claude invocations (the headless loop) ride. Its child
    env sets ``CLAUDE_CODE_OAUTH_TOKEN`` and removes ``ANTHROPIC_API_KEY``. It draws
    no per-token bill but shares the plan's depleting 5h/7d usage window with the
    main loop — so a right-sized eval lane + per-account routing (below) keep it from
    throttling that window / starving the loop.

    It has NO default ``pass`` path: it resolves ONLY from the env var or an injected
    per-account ``pass_path_override`` — selected from the ``anthropic_oauth_pass_paths``
    routing list by ``teatree.credential_config.resolve_subscription_credential`` — so
    eval load spreads across multiple subscription accounts. With neither an env value
    nor a configured account, :meth:`resolve` fails loud (naming ``anthropic_oauth_pass_paths``)
    rather than reading a dead built-in entry.

    It additionally FORBIDS :data:`ANTHROPIC_BASE_URL_ENV`: plan auth is only valid
    against Anthropic's own endpoint, and both the SDK and the ``claude`` CLI read that
    variable natively from an inherited env — so an ambient value would otherwise
    redirect a plan-authenticated child at an arbitrary host with nothing failing loud.
    :meth:`~Credential.child_env` raises instead of stripping, because the operator who
    exported it meant something by it and deserves to be told which credential refused.
    """

    spec = CredentialSpec(
        env_var="CLAUDE_CODE_OAUTH_TOKEN",
        conflicting_vars=("ANTHROPIC_API_KEY",),
        pass_path=None,
        routing_setting="anthropic_oauth_pass_paths",
        forbidden_vars=(ANTHROPIC_BASE_URL_ENV,),
    )


class OrcaRouterCredential(Credential):
    """The OrcaRouter BYOK metered API key — the ``pydantic_ai`` harness's Layer-2 provider.

    Layer-2 provider/credential resolution table (Layer 1 = ``agent_harness``,
    constraining which Layer-2 provider is valid — the ``ORCA_ROUTER_BYOK``
    member of :class:`~teatree.config.AgentHarnessProvider`, which encodes this
    same table as :meth:`~teatree.config.AgentHarnessProvider.valid_for`
    [#2885](https://github.com/souliane/teatree/issues/2885),
    [#2887](https://github.com/souliane/teatree/issues/2887)):

    | Layer 1          | Layer 2 provider    | Credential                        | Status       |
    |-------------------|----------------------|------------------------------------|---------------|
    | ``claude_sdk``     | subscription-OAuth   | ``AnthropicSubscriptionCredential`` | shipped (default) |
    | ``claude_sdk``     | API-key              | ``AnthropicApiKeyCredential``       | shipped (opt-in)  |
    | ``pydantic_ai``    | OrcaRouter BYOK       | ``OrcaRouterCredential`` (here)     | shipped (default) |
    | ``pydantic_ai``    | Vertex                | -- reserved --                      | not implemented   |

    ``claude_sdk`` never touches this credential — the bundled CLI only
    understands the two Anthropic credentials above. ``pydantic_ai`` never
    touches the Anthropic credentials — the ``CLAUDE_CODE_OAUTH_TOKEN`` is
    meaningless outside the bundled CLI, so its ONLY implemented Layer-2
    provider today is this metered BYOK key; a future Vertex AI binding is
    reserved but not yet built (mirrors the :class:`~teatree.agents.harness.PydanticAiHarness`
    precedent of shipping one path first).

    OrcaRouter is a separate, orthogonal provider (not an Anthropic account), so
    unlike the two credentials above it declares no ``conflicting_vars`` — nothing
    to strip when it is applied. It has NO built-in default ``pass`` path and no
    per-account routing LIST (unlike ``anthropic_oauth_pass_paths`` /
    ``anthropic_api_key_pass_paths``): BYOK means the operator supplies exactly one key,
    via the ``ORCA_ROUTER_API_KEY`` env var or the single ``orca_router_pass_path``
    setting (injected as ``pass_path_override``). With neither, :meth:`resolve` fails
    loud naming ``orca_router_pass_path`` rather than reading a dead built-in entry.
    """

    spec = CredentialSpec(
        env_var="ORCA_ROUTER_API_KEY",
        conflicting_vars=(),
        pass_path=None,
        routing_setting="orca_router_pass_path",
    )


_ORCA_ROUTER_BASE_URL_ENV = "ORCA_ROUTER_BASE_URL"


@dataclasses.dataclass(frozen=True)
class OrcaRouterProviderConfig:
    """The OpenAI-compatible provider config :class:`~teatree.agents.harness.PydanticAiHarness` needs.

    *api_key* rides :class:`OrcaRouterCredential` (env then ``pass``, same
    mechanics as every other :class:`Credential`). *base_url* is NOT a secret —
    it skips the ``pass`` store and reads ``ORCA_ROUTER_BASE_URL`` only, with NO
    fabricated default: a wrong hardcoded endpoint would silently route real
    spend at the wrong host, so an absent value fails loud instead of guessing.
    """

    api_key: str
    base_url: str


def resolve_orca_router_provider_config(*, credential: OrcaRouterCredential | None = None) -> OrcaRouterProviderConfig:
    """Resolve the OrcaRouter provider config, failing loud when either half is absent.

    *credential* is injectable (default: a fresh :class:`OrcaRouterCredential`,
    env then ``pass``) so callers can drive this with fake sources in tests, the
    same DI pattern every other credential resolver in this module uses.
    """
    credential = credential if credential is not None else OrcaRouterCredential()
    base_url = os.environ.get(_ORCA_ROUTER_BASE_URL_ENV, "").strip()
    if not base_url:
        msg = (
            f"no {_ORCA_ROUTER_BASE_URL_ENV} configured. Set the OrcaRouter OpenAI-compatible "
            "endpoint in the environment before selecting agent_harness=pydantic_ai."
        )
        raise CredentialError(msg)
    return OrcaRouterProviderConfig(api_key=credential.resolve(), base_url=base_url)
