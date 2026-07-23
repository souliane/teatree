"""Per-host scanner-job fan-out — the multi-host / multi-overlay attribution concern.

Split out of :mod:`teatree.loop.scanner_factories` (module-health cap): the
``_*_scanner_for`` builders there construct ONE scanner from an overlay's config,
whereas this module answers a different question — for a given overlay, which jobs
does each of its code hosts get, and which URL claims does a sibling overlay hold
more specifically? ``scanner_factories`` re-exports both names, so ``tick`` and
``domain_jobs`` import them exactly as before.
"""

import logging

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanner_factory_config import (
    _gitlab_approvals_enabled,
    _user_identity_aliases_for_overlay,
    stranger_pr_admission,
)
from teatree.loop.scanners import (
    GitLabApprovalsScanner,
    MyPrsScanner,
    ReviewerPrsScanner,
    TicketCompletionScanner,
    TicketDispositionScanner,
)
from teatree.loop.tick_resolvers import (
    _allowed_url_prefixes_for_host,
    _identity_alias_groups_for_overlay,
    _web_origin_for_host,
)

logger = logging.getLogger(__name__)


def _jobs_for_backend_hosts(
    backend: OverlayBackends,
    tag: str,
    *,
    all_backends: tuple[OverlayBackends, ...] = (),
) -> list[_ScannerJob]:
    """Build one scanner-job fan-out per host on *backend* (#976).

    Pre-fix the caller assumed one ``backend.host``; with multi-host the
    same fan-out must run for each platform that resolved a credential.
    ``TicketCompletionScanner`` is overlay-scoped (reads local Ticket
    rows), so it's emitted exactly once even when two hosts are present.

    *all_backends* (when provided) lets each scanner know the URL claims
    of sibling overlays so a less-specific claim here yields to a more
    specific claim there — see :func:`_competing_url_prefixes` (#1324).
    """
    jobs: list[_ScannerJob] = []
    ticket_completion_emitted = False
    gitlab_approvals_enabled = _gitlab_approvals_enabled()
    identity_groups = _identity_alias_groups_for_overlay(tag, backend)
    # #1113 Defect 1: the trusted operator identity set (``backend.identities``,
    # #976) is an implicit self-group when no explicit ``identity_aliases``
    # config overrides it. Without this union, ``user_identity_aliases`` and
    # ``identity_alias_groups`` both resolve to empty in the user's deployment
    # → ``_is_self_handoff`` short-circuits to False → same-human reassigns
    # between ``backend.identities`` members (the multi-identity operator set)
    # render as ``reassigned`` churn. Explicit groups still take precedence.
    if not identity_groups and len(backend.identities) > 1:
        identity_groups = (tuple(backend.identities),)
    reviewer_trusted, reviewer_admit_label = stranger_pr_admission(tag)
    for code_host in backend.hosts:
        url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
        competing_prefixes = _competing_url_prefixes(
            this_backend=backend,
            code_host=code_host,
            all_backends=all_backends,
        )
        jobs.extend(
            [
                _ScannerJob(
                    scanner=MyPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        allowed_url_prefixes=url_prefixes,
                        competing_url_prefixes=competing_prefixes,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=ReviewerPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        overlay_name=tag,
                        allowed_url_prefixes=url_prefixes,
                        competing_url_prefixes=competing_prefixes,
                        trusted_authors=reviewer_trusted,
                        admit_label=reviewer_admit_label,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=TicketDispositionScanner(
                        host=code_host,
                        overlay=backend.overlay,
                        ready_labels=backend.ready_labels,
                        overlay_name=tag,
                        user_identity_aliases=_user_identity_aliases_for_overlay(tag),
                        identity_alias_groups=identity_groups,
                    ),
                    overlay=tag,
                ),
            ],
        )
        if backend.overlay is not None and not ticket_completion_emitted:
            jobs.append(
                _ScannerJob(
                    scanner=TicketCompletionScanner(
                        overlay=backend.overlay,
                        overlay_name=tag,
                    ),
                    overlay=tag,
                ),
            )
            ticket_completion_emitted = True
        if gitlab_approvals_enabled:
            # Poll-driven complement to the webhook-driven `SCHEDULE_MERGE` path
            # (#936). Off by default — opt-in via the env flag so deployments
            # that already wire the GitLab webhook do not double-emit.
            jobs.append(
                _ScannerJob(
                    scanner=GitLabApprovalsScanner(host=code_host, identities=backend.identities),
                    overlay=tag,
                ),
            )
    return jobs


def _competing_url_prefixes(
    *,
    this_backend: OverlayBackends,
    code_host: CodeHostBackend,
    all_backends: tuple[OverlayBackends, ...],
) -> tuple[str, ...]:
    """Collect URL claims from every overlay OTHER than *this_backend* (#1324).

    Lets a scanner reject a URL it claims less specifically than a sibling
    overlay claims — the most-specific overlay attribution wins, so a
    dogfooding overlay that lists a sibling's repo path under
    ``workspace_repos`` no longer steals the sibling's PRs from its zone.

    Only sibling backends with a code-host that resolves to the same web
    origin contribute claims; a GitLab-only sibling can't compete for a
    GitHub URL.
    """
    if not all_backends:
        return ()
    own_origin = _web_origin_for_host(code_host)
    if not own_origin:
        return ()
    prefixes: list[str] = []
    for sibling in all_backends:
        if sibling is this_backend or sibling.name == this_backend.name:
            continue
        for sibling_host in sibling.hosts:
            if _web_origin_for_host(sibling_host) != own_origin:
                continue
            prefixes.extend(_allowed_url_prefixes_for_host(sibling, sibling_host))
    return tuple(prefixes)
