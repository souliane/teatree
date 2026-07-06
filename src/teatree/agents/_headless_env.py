"""Child-process credential env for a ``claude_sdk`` headless dispatch.

Split out of :mod:`teatree.agents.headless` for the module-health LOC cap: the
Layer-2 ``agent_harness_provider``-keyed credential resolution (#2887) plus the overlay
scope the per-account selector routes for. Re-exported from ``teatree.agents.headless``
so ``from teatree.agents.headless import _provider_child_env`` stays valid.
"""

from teatree.config import AgentHarness, AgentHarnessProvider
from teatree.core.models import Task
from teatree.credential_config import resolve_api_key_credential, resolve_subscription_credential
from teatree.llm.credentials import CredentialError
from teatree.utils.git_run import git_env_without_overrides


def _overlay_scope(task: Task) -> str:
    """The overlay the credential selector routes for — the task's ticket overlay.

    Empty (the ``GLOBAL_SCOPE`` sentinel) when the ticket carries no overlay, so the
    selector falls back to the global routing list.
    """
    return task.ticket.overlay or ""


def _provider_child_env(provider: AgentHarnessProvider | None, *, scope: str = "") -> dict[str, str] | None:
    """The child-process env that pins the Layer-2 credential for a ``claude_sdk`` dispatch (#2887).

    ``provider is None`` (the default — no explicit Layer-2 pin) returns
    ``None``: the ambient environment is used UNCHANGED, so an operator who
    never configured ``agent_harness_provider`` is never forced through an
    eager credential lookup — the ``claude`` CLI's own ambient auth state
    (however it was set up) applies, exactly as before #2887. An explicit
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
