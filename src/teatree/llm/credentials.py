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
Which one the automated eval lane rides is ``agent_harness_provider``'s call,
resolved through ``teatree.credential_config.resolve_eval_credential``: the default
is the subscription credential, with the metered key selectable per run via
``t3 eval run --credential api_key``. The
subscription credential also backs the non-eval Claude invocations (the headless
loop). This module stays foundation-pure — it enforces "use THIS credential,
exclusively", not which one the eval lane picks.

The pieces are named provider-agnostically (``Credential`` / ``CredentialSpec`` /
``CredentialSource``) so this layer IS the ``LLMBackend.credential``: Claude here,
and every OpenAI-compatible provider through one generic backend in
:mod:`teatree.llm.openai_compatible`, configured by a base URL, a model name and a
credential-store entry name rather than a class per provider
([#3666](https://github.com/souliane/teatree/issues/3666)). Dependency-injected
sources keep the whole surface unit-testable without touching the real environment
or the ``pass`` store.

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
        raise CredentialError(
            base_url_refusal(
                present,
                authenticator=f"{self.spec.env_var} authenticates against the Anthropic subscription",
                remedy="pin agent_harness_provider=api_key to route a metered key through that endpoint",
            )
        )

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


def base_url_refusal(present: Sequence[str], *, authenticator: str, remedy: str) -> str:
    """The shared wording for every base-URL redirect refusal.

    Three seams enforce this policy over different inputs — a credential's own
    ``child_env`` mapping, the eval lane's ambient env, and the unpinned-spawn
    guard — but an operator reading any of them needs the same two facts: which
    variable is refused, and what to do instead. Templated here so a changed
    remedy cannot land in one seam and go stale in the other two.

    *authenticator* names what is authenticating and why the redirect is refused;
    *remedy* is the alternative, phrased to follow "or".
    """
    names = ", ".join(present)
    return (
        f"{names} is set, but {authenticator}, which is only valid against Anthropic's own "
        f"endpoint. Redirecting a plan-authenticated child at another endpoint is refused. "
        f"Either unset {names}, or {remedy}."
    )


class AnthropicApiKeyCredential(Credential):
    """The metered Anthropic API key — strips the subscription OAuth token.

    The metered credential the eval lane rides under an ``api_key`` provider
    (per-token cost, no usage window). Its child env sets
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


def reject_ambient_base_url_redirect() -> None:
    """Refuse an ambient-auth ``claude`` spawn that also carries a base-URL redirect.

    The guard for every AUTONOMOUS seam that spawns a ``claude`` child WITHOUT
    pinning a credential onto its env — the headless dispatch's unpinned default,
    the clean-room one-shot turn, and the maker pane. Those children authenticate
    however the CLI's own login state resolves, which this process cannot observe,
    and both the CLI and the Anthropic SDK read
    :data:`ANTHROPIC_BASE_URL_ENV` from the inherited env.

    Also called by ``t3 agent`` on its ``-p`` branch: that exec replaces this
    process, but the child runs headless with nobody watching its auth behaviour,
    which is the condition this guard exists for — not whether the spawn is an exec.

    Deliberately NOT covered: the ``exec``-family spawns that leave a human in front
    of the child — bare interactive ``t3 agent``, ``t3 loop start``, and the
    dashboard's ttyd debug session. The operator sees the child's own auth behaviour
    directly there, so a refusal would only obscure an environment they set
    themselves.

    The one unambiguously sanctioned shape passes: a metered key with no
    subscription token beside it — an operator pointing their OWN API key at a
    gateway, Bedrock/Vertex, or an Anthropic-compatible third-party provider. Every
    other combination raises: a subscription token present, both present, or neither
    (the CLI falls back to its stored login, which on a plan deployment is the
    subscription).

    Seams that DO pin a credential need no call here — they build their env through
    :meth:`Credential.child_env`, whose ``forbidden_vars`` rule refuses the same
    combination at the credential itself.
    """
    if not os.environ.get(ANTHROPIC_BASE_URL_ENV, "").strip():
        return
    has_api_key = bool(os.environ.get(AnthropicApiKeyCredential.spec.env_var, "").strip())
    has_subscription = bool(os.environ.get(AnthropicSubscriptionCredential.spec.env_var, "").strip())
    if has_api_key and not has_subscription:
        return
    raise CredentialError(
        base_url_refusal(
            [ANTHROPIC_BASE_URL_ENV],
            authenticator=(
                "no credential is pinned, so the spawned claude CLI authenticates with whatever "
                "login state it holds — on a subscription deployment that is plan auth"
            ),
            remedy=(
                "pin agent_harness_provider=api_key so a metered key routes through that endpoint deterministically"
            ),
        )
    )
