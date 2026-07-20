"""Child-process credential env for a ``claude_sdk`` headless dispatch.

Split out of :mod:`teatree.agents.headless` for the module-health LOC cap: the
Layer-2 ``agent_harness_provider``-keyed credential resolution (#2887) plus the overlay
scope the per-account selector routes for. Re-exported from ``teatree.agents.headless``
so ``from teatree.agents.headless import _provider_child_env`` stays valid.
"""

import logging
import os

from teatree.config import AgentHarness, AgentHarnessProvider, get_effective_settings
from teatree.core.models import Task
from teatree.credential_config import resolve_api_key_credential, resolve_subscription_credential
from teatree.llm.credentials import (
    ANTHROPIC_BASE_URL_ENV,
    AnthropicApiKeyCredential,
    AnthropicSubscriptionCredential,
    CredentialError,
)
from teatree.utils.git_run import git_env_without_overrides

logger = logging.getLogger(__name__)


def _overlay_scope(task: Task) -> str:
    """The overlay the credential selector routes for — the task's ticket overlay.

    Empty (the ``GLOBAL_SCOPE`` sentinel) when the ticket carries no overlay, so the
    selector falls back to the global routing list.
    """
    return task.ticket.overlay or ""


def _reject_ambient_base_url_redirect() -> None:
    """Refuse an ambient-auth dispatch that also carries a base-URL redirect.

    With no Layer-2 pin the spawned ``claude`` CLI authenticates however its own
    login state resolves — which this process cannot observe. Both the CLI and the
    Anthropic SDK read :data:`~teatree.llm.credentials.ANTHROPIC_BASE_URL_ENV` from
    the inherited env, so an ambient value silently redirects that child.

    The one shape that is unambiguously sanctioned is a metered key with no
    subscription token beside it: an operator pointing their OWN API key at a
    gateway, Bedrock/Vertex, or an Anthropic-compatible third-party provider. That
    passes. Every other combination — a subscription token present, both present,
    or neither (the CLI falls back to its stored login, which on a plan deployment
    is the subscription) — refuses, because the redirect would or might carry plan
    auth to a non-Anthropic endpoint.

    The pinned-provider paths need no check here: they build their env through
    :meth:`~teatree.llm.credentials.Credential.child_env`, whose ``forbidden_vars``
    rule already refuses the same combination at the credential itself.
    """
    if not os.environ.get(ANTHROPIC_BASE_URL_ENV, "").strip():
        return
    has_api_key = bool(os.environ.get(AnthropicApiKeyCredential.spec.env_var, "").strip())
    has_subscription = bool(os.environ.get(AnthropicSubscriptionCredential.spec.env_var, "").strip())
    if has_api_key and not has_subscription:
        return
    msg = (
        f"{ANTHROPIC_BASE_URL_ENV} is set and no agent_harness_provider is pinned, so the "
        f"spawned claude CLI would be redirected while authenticating with whatever login "
        f"state it holds — which on a subscription deployment is plan auth, valid only "
        f"against Anthropic's own endpoint. Either unset {ANTHROPIC_BASE_URL_ENV}, or pin "
        f"agent_harness_provider=api_key so a metered key routes through that endpoint "
        f"deterministically."
    )
    raise CredentialError(msg)


def _provider_child_env(provider: AgentHarnessProvider | None, *, scope: str = "") -> dict[str, str] | None:
    """The child-process env that pins the Layer-2 credential for a ``claude_sdk`` dispatch (#2887).

    ``provider is None`` (the default — no explicit Layer-2 pin) returns
    ``None``: the ambient environment is used UNCHANGED, so an operator who
    never configured ``agent_harness_provider`` is never forced through an
    eager credential lookup — the ``claude`` CLI's own ambient auth state
    (however it was set up) applies, exactly as before #2887. The ONE ambient
    combination refused is a base-URL redirect that would carry unobservable
    (and on a plan deployment, subscription) auth to a non-Anthropic endpoint —
    see :func:`_reject_ambient_base_url_redirect`. An explicit
    ``api_key`` forces the metered ``ANTHROPIC_API_KEY`` (stripping the
    subscription token); an explicit ``subscription_oauth`` forces the
    subscription ``CLAUDE_CODE_OAUTH_TOKEN`` (stripping the API key) so the
    spawned ``claude`` CLI rides the plan, not the meter. *scope* is the
    overlay the per-account routing selector picks an account for, so two
    overlays ride distinct subscription accounts. The sole caller
    (``_resolve_child_env_or_failure``) is already scoped to a
    :class:`~teatree.agents.harness.ClaudeSdkHarness` dispatch, so a
    NON-``None`` *provider* must be a Layer-2 provider valid under Layer 1
    ``agent_harness=claude_sdk`` (:meth:`~teatree.config.AgentHarnessProvider.valid_for`) —
    an ``orca_router_byok`` provider reaching here is a genuine cross-layer
    misconfiguration and raises :class:`CredentialError` loud rather than
    silently falling through to the ambient env. Also raises when the selected
    token resolves from neither the env nor the ``pass`` store (or every
    configured account is exhausted), so a misconfigured headless run always
    fails loud.
    """
    if provider is None:
        _reject_ambient_base_url_redirect()
        return None
    valid = AgentHarnessProvider.valid_for(AgentHarness.CLAUDE_SDK)
    if provider not in valid:
        msg = (
            f"agent_harness_provider={provider.value!r} is not valid under agent_harness=claude_sdk; "
            f"valid values: {', '.join(sorted(p.value for p in valid))}"
        )
        raise CredentialError(msg)
    # Pin the credential onto a GIT_*-stripped base so ``options.env`` cannot
    # re-introduce an outer git hook's GIT_DIR/GIT_INDEX_FILE (the SDK merges
    # ``options.env`` over the inherited env, so a GIT_* here would reach the child).
    base = git_env_without_overrides()
    if provider is AgentHarnessProvider.API_KEY:
        return resolve_api_key_credential(scope=scope).child_env(base)
    return resolve_subscription_credential(scope=scope).child_env(base)


def system_child_env() -> dict[str, str] | None:
    """The ``claude`` CLI child env for a SYSTEM pass — no Task, global scope.

    A system pass (the dream distiller / eval synthesizer) spawns ``claude`` outside
    any ticket, so it has no overlay to route an account for and resolves the Layer-2
    ``agent_harness_provider`` credential at the GLOBAL scope. Behaviour mirrors
    :func:`_provider_child_env`: ``None`` (no Layer-2 pin) returns ``None`` — the
    ambient ``claude`` auth state applies unchanged. An explicit ``subscription_oauth``
    / ``api_key`` provider pins that credential (on the GIT_*-stripped base) so the
    spawned CLI rides the plan or the meter deterministically rather than whatever the
    ambient env happens to hold. A :class:`CredentialError` (the pinned token resolves
    from neither env nor the ``pass`` store) PROPAGATES so an auth gap fails the pass
    loud instead of laundering into a fake empty/unparsable result.

    Unlike :func:`_provider_child_env` — whose sole caller is already scoped to a
    ``claude_sdk`` dispatch, so a non-``claude_sdk`` provider there is a real
    misconfiguration — this helper's callers spawn ``claude`` unconditionally on ANY
    ``agent_harness``. A provider valid only under ``pydantic_ai`` (a validly
    configured deployment) must therefore fall back to the ambient env with a WARNING,
    not raise: the system ``claude`` turn stays on whatever auth the ambient env
    carries, exactly as before this pinning existed.
    """
    provider = get_effective_settings().agent_harness_provider
    if provider is None:
        _reject_ambient_base_url_redirect()
        return None
    if provider not in AgentHarnessProvider.valid_for(AgentHarness.CLAUDE_SDK):
        logger.warning(
            "agent_harness_provider=%s pins a non-claude_sdk lane; the system claude "
            "subprocess falls back to the ambient environment's auth state",
            provider.value,
        )
        _reject_ambient_base_url_redirect()
        return None
    return _provider_child_env(provider, scope="")
