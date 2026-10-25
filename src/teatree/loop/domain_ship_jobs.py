from teatree.backends.github.client import GitHubCodeHost
from teatree.backends.gitlab.client import GitLabCodeHost
from teatree.core.backend_factory import OverlayBackends
from teatree.core.merge.host_kind import forge_for_repo_slug
from teatree.core.mode_resolution import egress_forbidden
from teatree.core.overlay_repos import owned_repo_slugs
from teatree.loop.job_identity import Domain, _ScannerJob
from teatree.loop.scanner_factories import (
    _competing_url_prefixes,
    _mr_conflict_scanner_for,
    _mr_triage_scanner_for,
    _pr_sweep_scanner_for,
)
from teatree.loop.scanner_factory_config import _user_slack_id_for_overlay
from teatree.loop.scanners import GitLabApprovalsScanner, MyPrsScanner
from teatree.loop.scanners.my_prs_ci import BoundedCiEnricher
from teatree.loop.scanners.pr_findings import RecordedCriticFindingReader, RecordedVerdictReader
from teatree.loop.scanners.unaudited_merge import UnauditedMergeScanner
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host
from teatree.loops.enable_verdict import loop_admits
from teatree.types import ScannerError, ScannerErrorClass


def _colleague_egress_selected(backend: OverlayBackends) -> bool:
    return backend.messaging is not None and not egress_forbidden()


def _review_request_sender_runs(backend: OverlayBackends) -> bool:
    return _colleague_egress_selected(backend) and loop_admits(Domain.FOLLOWUP.value)


def _ship_jobs_for_overlay(
    backend: OverlayBackends,
    *,
    all_backends: tuple[OverlayBackends, ...],
) -> list[_ScannerJob]:
    tag = backend.name
    jobs: list[_ScannerJob] = []
    ci_enricher = BoundedCiEnricher()
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
    for repo in owned_repo_slugs(backend.overlay):
        forge = forge_for_repo_slug(repo)
        host = next(
            (
                candidate
                for candidate in backend.hosts
                if (forge == "github" and isinstance(candidate, GitHubCodeHost))
                or (forge == "gitlab" and isinstance(candidate, GitLabCodeHost))
            ),
            backend.hosts[0] if len(backend.hosts) == 1 else None,
        )
        if host is None:
            raise ScannerError(
                scanner="unaudited_merge",
                error_class=ScannerErrorClass.UNKNOWN,
                detail=f"no unambiguous code host for {repo}",
            )
        jobs.append(_ScannerJob(scanner=UnauditedMergeScanner(host=host, repos=(repo,), overlay=tag), overlay=tag))
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
