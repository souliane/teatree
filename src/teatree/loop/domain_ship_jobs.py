import logging

from teatree.backends.github.client import GitHubCodeHost
from teatree.backends.gitlab.client import GitLabCodeHost
from teatree.config import Autonomy
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.merge.host_kind import forge_for_repo_slug
from teatree.core.mode_resolution import egress_forbidden
from teatree.core.overlay_repos import owned_repo_slugs
from teatree.loop.job_identity import Domain, _ScannerJob
from teatree.loop.scanner_factories import (
    _competing_url_prefixes,
    _effective_settings_for_overlay,
    _mr_conflict_scanner_for,
    _mr_triage_scanner_for,
    _pr_sweep_scanner_for,
)
from teatree.loop.scanner_factory_config import _user_identity_aliases_for_overlay, _user_slack_id_for_overlay
from teatree.loop.scanners import GitLabApprovalsScanner, MyPrsScanner
from teatree.loop.scanners.my_prs_ci import BoundedCiEnricher
from teatree.loop.scanners.pr_findings import RecordedCriticFindingReader, RecordedVerdictReader
from teatree.loop.scanners.unaudited_merge import UnauditedMergeScanner
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host
from teatree.loops.enable_verdict import loop_admits

logger = logging.getLogger(__name__)


def _colleague_egress_selected(backend: OverlayBackends) -> bool:
    """Whether this tick selects the scanners that act outward under the owner's name."""
    return backend.messaging is not None and not egress_forbidden()


def _review_request_sender_runs(backend: OverlayBackends) -> bool:
    """Whether the followup loop will send what the triage ladder finds owed, so SHIP need not ask."""
    return _colleague_egress_selected(backend) and loop_admits(Domain.FOLLOWUP.value)


def _host_for_repo(backend: OverlayBackends, repo: str) -> CodeHostBackend | None:
    forge = forge_for_repo_slug(repo)
    return next(
        (
            candidate
            for candidate in backend.hosts
            if (forge == "github" and isinstance(candidate, GitHubCodeHost))
            or (forge == "gitlab" and isinstance(candidate, GitLabCodeHost))
        ),
        backend.hosts[0] if len(backend.hosts) == 1 else None,
    )


def _unaudited_merge_jobs(backend: OverlayBackends) -> list[_ScannerJob]:
    tag = backend.name
    try:
        repos = owned_repo_slugs(backend.overlay)
        solo_overlay = _effective_settings_for_overlay(tag).autonomy is Autonomy.FULL
        identities = _user_identity_aliases_for_overlay(tag)
    except Exception:
        logger.exception("unaudited_merge: cannot resolve the watch scope for %s; no detector this tick", tag)
        return []
    jobs: list[_ScannerJob] = []
    for repo in repos:
        if (host := _host_for_repo(backend, repo)) is None:
            logger.warning("unaudited_merge: no unambiguous code host for %s; not watched", repo)
            continue
        detector = UnauditedMergeScanner(
            host=host, repos=(repo,), overlay=tag, identities=identities, solo_overlay=solo_overlay
        )
        jobs.append(_ScannerJob(scanner=detector, overlay=tag))
    return jobs


def _ship_jobs_for_overlay(
    backend: OverlayBackends,
    *,
    all_backends: tuple[OverlayBackends, ...],
) -> list[_ScannerJob]:
    """Own-author PR scanner + the auto-merge PR sweep + the GitLab-approvals poll, per host."""
    tag = backend.name
    jobs: list[_ScannerJob] = []
    # One enricher for the whole overlay: its per-tick budget is shared across the
    # hosts below rather than multiplied by them, and this builder runs once a tick.
    ci_enricher = BoundedCiEnricher()
    for code_host in backend.hosts:
        url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
        competing_prefixes = _competing_url_prefixes(
            this_backend=backend,
            code_host=code_host,
            all_backends=all_backends,
        )
        # Every open PR owes a resolved conflict whatever its review policy
        # says, so the sweep rides the ship domain alongside the merge engine rather
        # than the colleague-facing review loop the away posture skips.
        jobs.extend(
            [
                _ScannerJob(
                    scanner=MyPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        allowed_url_prefixes=url_prefixes,
                        competing_url_prefixes=competing_prefixes,
                        ci_enricher=ci_enricher,
                        verdict_reader=RecordedVerdictReader(),
                        critic_reader=RecordedCriticFindingReader(),
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=GitLabApprovalsScanner(
                        host=code_host,
                        identities=backend.identities,
                        allowed_url_prefixes=url_prefixes,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(scanner=_mr_conflict_scanner_for(backend, code_host), overlay=tag),
            ]
        )
    jobs.extend(_unaudited_merge_jobs(backend))
    sweep_scanner = _pr_sweep_scanner_for(backend, slack_user_id=_user_slack_id_for_overlay(tag))
    if sweep_scanner is not None:
        jobs.append(_ScannerJob(scanner=sweep_scanner, overlay=tag))
    triage_scanner = _mr_triage_scanner_for(
        backend,
        ci_enricher=ci_enricher,
        ask_owner_on_missing_review=not _review_request_sender_runs(backend),
    )
    if triage_scanner is not None:
        jobs.append(_ScannerJob(scanner=triage_scanner, overlay=tag))
    return jobs
