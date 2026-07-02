r"""Provider-neutral credential resolution for Claude SDK / bundled-CLI invocations.

THE canonical way to authenticate any Claude SDK or bundled-``claude``-CLI
invocation in teatree. A :class:`Credential` resolves a single secret from an
ordered list of injected :class:`CredentialSource`\ s (env wins, then the
``pass`` store), fails loud with a :class:`CredentialError` naming the exact fix
when neither yields a value, and builds a child process env that sets its own env
var while **stripping** every conflicting credential — so a metered invocation can
never silently fall back to a different credential.

The first applied rule (:class:`AnthropicApiKeyCredential`) enforces the
API-exclusive metered-eval policy ([#2707](https://github.com/souliane/teatree/issues/2707)):
the metered eval lane authenticates EXCLUSIVELY via ``ANTHROPIC_API_KEY`` and the
child env strips the subscription ``CLAUDE_CODE_OAUTH_TOKEN`` (the bundled CLI
prefers the API key only when the OAuth token is absent). The subscription
counterpart (:class:`AnthropicSubscriptionCredential`) is the inverse, for the
non-eval Claude invocations that legitimately ride the subscription.

The pieces are named provider-agnostically (``Credential`` / ``CredentialSpec`` /
``CredentialSource``) so this layer later becomes an ``LLMBackend.credential`` —
Claude today, other providers (OpenRouter, …) later — with no rework at the call
sites. Dependency-injected sources keep the whole surface unit-testable without
touching the real environment or the ``pass`` store.

A credential's ``pass_path`` MAY be overridden per instance by an INJECTED
``pass_path_override`` (a plain string) — the resolved per-account ``pass`` entry
an operator routes to. This module stays foundation-pure: it never reads the
config store itself. The DB-config read (``ConfigSetting``, the per-overlay routing
lists ``anthropic_oauth_pass_paths`` / ``anthropic_api_key_pass_paths``, overlay
scope then global) lives in the domain-layer factory ``teatree.credential_config``,
whose selector picks a healthy account and injects its ``pass`` path here. ``spec``
stays a static constant
the module-load consumers (``cli/eval/docker.py``, ``eval/isolation.py``) read
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
    and states that metered evals never fall back to the subscription credential,
    so the user is never left guessing why the run refused.
    """


@dataclasses.dataclass(frozen=True)
class CredentialSpec:
    """The identity of one credential: its env var, ``pass`` path, and conflicts.

    *conflicting_vars* are the credentials that must be REMOVED from a child env
    when this one is applied — the bundled CLI prefers one credential only when
    the others are absent, so stripping the conflicts is what makes "use THIS
    credential, exclusively" actually hold.
    """

    env_var: str
    pass_path: str
    conflicting_vars: tuple[str, ...]


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

    A missing entry (or ``pass`` not installed) resolves to ``None`` — handled as
    absent, never a crash. :func:`~teatree.utils.secrets.read_pass` already
    swallows the non-zero ``pass show`` exit and returns ``""`` for an absent
    entry, which this normalizes to ``None``.
    """

    def lookup(self, spec: "CredentialSpec") -> str | None:  # noqa: PLR6301 — instance method to satisfy the CredentialSource Protocol.
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
    """

    spec: CredentialSpec

    def __init__(
        self,
        *,
        sources: Sequence[CredentialSource] = _DEFAULT_SOURCES,
        pass_path_override: str | None = None,
    ) -> None:
        self._sources = tuple(sources)
        self._pass_path_override = pass_path_override

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
        raise CredentialError(self._missing_message(spec))

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
        conflicting one. *base* is never mutated. Raises :class:`CredentialError`
        when no value is resolvable (the loud refusal propagates to the caller).
        """
        value = self.resolve()
        child = dict(base)
        for conflicting in self.spec.conflicting_vars:
            child.pop(conflicting, None)
        child[self.spec.env_var] = value
        return child

    @staticmethod
    def _missing_message(spec: CredentialSpec) -> str:
        return (
            f"no {spec.env_var} credential available. Set {spec.env_var} in the "
            f"environment, or store it locally with `pass insert {spec.pass_path}`. "
            "Metered evals authenticate EXCLUSIVELY via the metered API key — they never "
            "fall back to the subscription OAuth token (a full run would throttle it)."
        )


class AnthropicApiKeyCredential(Credential):
    """The metered Anthropic API key — strips the subscription OAuth token.

    The credential the metered eval lane authenticates with EXCLUSIVELY ([#2707]).
    Its child env sets ``ANTHROPIC_API_KEY`` and removes ``CLAUDE_CODE_OAUTH_TOKEN``
    so the SDK / bundled CLI can never bill the subscription. The ``pass_path``
    default (``anthropic/api-key``) is overridable per account via an injected
    ``pass_path_override`` — selected from the ``anthropic_api_key_pass_paths``
    routing list by ``teatree.credential_config.resolve_api_key_credential``.
    """

    spec = CredentialSpec(
        env_var="ANTHROPIC_API_KEY",
        pass_path="anthropic/api-key",  # noqa: S106 — pass key, not a secret
        conflicting_vars=("CLAUDE_CODE_OAUTH_TOKEN",),
    )


class AnthropicSubscriptionCredential(Credential):
    """The subscription OAuth token — strips the metered API key.

    The credential the NON-eval Claude invocations legitimately ride. Its child
    env sets ``CLAUDE_CODE_OAUTH_TOKEN`` and removes ``ANTHROPIC_API_KEY``. The
    ``pass_path`` default (``anthropic/oauth-token``) is overridable per account
    via an injected ``pass_path_override`` — selected from the
    ``anthropic_oauth_pass_paths`` routing list by
    ``teatree.credential_config.resolve_subscription_credential``.
    """

    spec = CredentialSpec(
        env_var="CLAUDE_CODE_OAUTH_TOKEN",
        pass_path="anthropic/oauth-token",  # noqa: S106 — pass key, not a secret
        conflicting_vars=("ANTHROPIC_API_KEY",),
    )
