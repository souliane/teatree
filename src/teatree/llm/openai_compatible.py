"""ONE backend for every OpenAI-compatible API (souliane/teatree#3666).

The router the ``pydantic_ai`` harness rides is an ordinary OpenAI-compatible
API, so it gets no code of its own: a base URL, a model name, and the NAME of a
credential-store entry, all generic settings
(``openai_compatible_base_url`` / ``openai_compatible_model`` /
``openai_compatible_credential_entry``). Pointing teatree at a different
OpenAI-compatible provider is a config change, never a new credential class.

Only the credential-store ENTRY NAME lives in config — the secret value is read
from the store at point of use through the same env-then-``pass``
:class:`~teatree.llm.credentials.Credential` machinery every other credential
uses.

Neither half is defaulted. A fabricated endpoint would silently route real spend
at the wrong host, so an absent base URL fails loud naming the setting to set —
the same late-fail contract the credential itself carries.
"""

import os
import sys
from dataclasses import dataclass

from teatree.llm.credentials import Credential, CredentialError, CredentialSpec

#: The generic API-key env var. Wins over the credential-store entry, so CI can
#: inject a key without a store.
OPENAI_COMPATIBLE_API_KEY_ENV = "OPENAI_COMPATIBLE_API_KEY"
#: The generic endpoint env var — the env layer of ``openai_compatible_base_url``.
OPENAI_COMPATIBLE_BASE_URL_ENV = "OPENAI_COMPATIBLE_BASE_URL"

_BASE_URL_SETTING = "openai_compatible_base_url"
_CREDENTIAL_ENTRY_SETTING = "openai_compatible_credential_entry"

#: Retired provider-specific env vars mapped onto their generic successor. A data
#: migration carries the DB rows (#3666); an env var lives outside the DB, so the
#: only honest handling is to say so out loud rather than let a still-exported
#: retired var read as "my configuration is in effect" (#3527).
_RETIRED_ENV_VARS: dict[str, str] = {
    "ORCA_ROUTER_BASE_URL": OPENAI_COMPATIBLE_BASE_URL_ENV,
    "ORCA_ROUTER_API_KEY": OPENAI_COMPATIBLE_API_KEY_ENV,
}


def warn_retired_env_vars() -> None:
    """Report any retired provider env var that is set while its successor is not."""
    for retired, successor in _RETIRED_ENV_VARS.items():
        if os.environ.get(retired, "").strip() and not os.environ.get(successor, "").strip():
            sys.stderr.write(
                f"WARNING: {retired} is set but is no longer read — the provider-specific backend "
                f"collapsed into the generic OpenAI-compatible one (#3666). Export {successor} instead; "
                f"until you do, this value has NO effect.\n"
            )


class OpenAICompatibleCredential(Credential):
    """The API key for whichever OpenAI-compatible provider is configured.

    Provider-neutral by construction: it declares no ``conflicting_vars`` (it is
    orthogonal to the Anthropic credentials, not a mirror image of them) and no
    built-in ``pass_path``. It resolves from :data:`OPENAI_COMPATIBLE_API_KEY_ENV`
    or from the credential-store entry the ``openai_compatible_credential_entry``
    setting names, injected as ``pass_path_override`` by the domain-layer factory;
    with neither, :meth:`~teatree.llm.credentials.Credential.resolve` fails loud
    naming that setting rather than reading a dead default entry.
    """

    spec = CredentialSpec(
        env_var=OPENAI_COMPATIBLE_API_KEY_ENV,
        conflicting_vars=(),
        pass_path=None,
        routing_setting=_CREDENTIAL_ENTRY_SETTING,
    )


@dataclass(frozen=True, slots=True)
class OpenAICompatibleBackend:
    """A fully-resolved OpenAI-compatible backend: where, which model, and with what key."""

    base_url: str
    model: str
    api_key: str


def resolve_openai_compatible_backend(
    *,
    base_url: str,
    model: str,
    credential: OpenAICompatibleCredential | None = None,
) -> OpenAICompatibleBackend:
    """Resolve the configured backend, failing loud when the endpoint is absent.

    *base_url* is the ``openai_compatible_base_url`` setting;
    :data:`OPENAI_COMPATIBLE_BASE_URL_ENV` fills it when the setting is empty, so a
    CI job can point the lane at a sandbox without a DB write. *credential* is
    injectable so callers drive this with fake sources in tests, the same DI
    pattern every other credential resolver uses.
    """
    warn_retired_env_vars()
    resolved_url = base_url.strip() or os.environ.get(OPENAI_COMPATIBLE_BASE_URL_ENV, "").strip()
    if not resolved_url:
        msg = (
            f"no OpenAI-compatible endpoint configured. Set the `{_BASE_URL_SETTING}` setting "
            f"(or {OPENAI_COMPATIBLE_BASE_URL_ENV} in the environment) before selecting "
            "agent_harness=pydantic_ai — teatree never guesses an endpoint, because a wrong one "
            "routes real spend at the wrong host."
        )
        raise CredentialError(msg)
    resolver = credential if credential is not None else OpenAICompatibleCredential()
    return OpenAICompatibleBackend(base_url=resolved_url, model=model, api_key=resolver.resolve())
