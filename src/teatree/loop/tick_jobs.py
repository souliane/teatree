"""Scanner-job construction for the loop tick.

Build the per-overlay fan-out of scanner jobs that ``run_tick``
executes in parallel. Split out of ``tick.py`` to keep the
orchestrator under the module-health LOC gate; the orchestrator
delegates to ``build_default_jobs`` and ``build_default_scanners``.
"""

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.config import discover_overlays, load_config

if TYPE_CHECKING:
    from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.scanners import (
    ActiveTicketsScanner,
    ArchitecturalReviewScanner,
    AssignedIssuesScanner,
    GitLabApprovalsScanner,
    IncomingEventsScanner,
    MyPrsScanner,
    NotionViewScanner,
    OutboundAuditScanner,
    PendingTasksScanner,
    ReviewerPrsScanner,
    ReviewNagScanner,
    Scanner,
    SlackDmInboundScanner,
    SlackMentionsScanner,
    SlackReviewIntentScanner,
    StaleTicketsScanner,
    TicketCompletionScanner,
    TicketDispositionScanner,
)
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host, _identity_alias_groups_for_overlay

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ScannerJob:
    """Internal record pairing a scanner with its overlay tag."""

    scanner: Scanner
    overlay: str


def _jobs_for_backend_hosts(backend: OverlayBackends, tag: str) -> list[_ScannerJob]:
    """Build one scanner-job fan-out per host on *backend* (#976).

    Pre-fix the caller assumed one ``backend.host``; with multi-host the
    same fan-out must run for each platform that resolved a credential.
    ``TicketCompletionScanner`` is overlay-scoped (reads local Ticket
    rows), so it's emitted exactly once even when two hosts are present.
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
    for code_host in backend.hosts:
        url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
        jobs.extend(
            [
                _ScannerJob(
                    scanner=MyPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        allowed_url_prefixes=url_prefixes,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=ReviewerPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        overlay_name=tag,
                        allowed_url_prefixes=url_prefixes,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=AssignedIssuesScanner(
                        host=code_host,
                        ready_labels=backend.ready_labels,
                        exclude_labels=backend.exclude_labels,
                        auto_start=backend.auto_start_assigned_issues,
                        max_concurrent=backend.max_concurrent_auto_starts,
                        overlay_name=tag,
                        identities=backend.identities,
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


def _architectural_review_scanner_for(backend: OverlayBackends) -> ArchitecturalReviewScanner | None:
    """Build a per-overlay architectural-review scanner from teatree-core config.

    #1136 / #1152 re-architecture: the architectural-review cadence is a
    teatree-core platform behaviour that applies uniformly to every
    overlay's worktrees, NOT a per-overlay opt-in. The settings live on
    :class:`teatree.config.UserSettings` (the ``[teatree]`` table in
    ``~/.teatree.toml``, with optional per-overlay overrides via the
    standard ``[overlays.<name>]`` shape — see
    ``OVERLAY_OVERRIDABLE_SETTINGS``). The scanner is instantiated once
    per registered overlay so each overlay's task queue gets its own
    cadence; a single core ``architectural_review_disabled = true``
    escape hatch suppresses scanning for the active overlay (and an
    overlay-scoped override allows pinning the toggle per-overlay).

    Returns ``None`` when the active overlay has
    ``architectural_review_disabled = true`` (the escape hatch).
    Unlike the previous wiring, this no longer skips overlays without a
    Python class — the scanner only needs ``backend.name`` to operate.
    """
    settings = _effective_settings_for_overlay(backend.name)
    if settings.architectural_review_disabled:
        return None
    return ArchitecturalReviewScanner(
        overlay_name=backend.name,
        skill=settings.architectural_review_skill,
        cadence_hours=settings.architectural_review_cadence_hours,
        after_merge_count=settings.architectural_review_after_merge_count,
    )


def _effective_settings_for_overlay(overlay_name: str) -> "UserSettings":
    """Resolve :class:`UserSettings` honouring this overlay's ``[overlays.<name>]`` overrides.

    Mirrors ``get_effective_settings()`` but resolves the active overlay
    explicitly by name (the scanner-builder loops over every registered
    overlay, not just the one in ``T3_OVERLAY_NAME``). Falls back to the
    global ``[teatree]`` values when no per-overlay override is set.
    """
    from dataclasses import replace  # noqa: PLC0415

    base = load_config().user
    for entry in discover_overlays():
        if entry.name == overlay_name and entry.overrides:
            return replace(base, **entry.overrides)
    return base


def _gitlab_approvals_enabled() -> bool:
    """Read the ``TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED`` feature flag.

    Default off — the scanner is poll-driven and overlaps with the webhook
    path; deployments that already wire ``/hooks/gitlab/`` do not need it.
    Returns True for any truthy value (``1``, ``true``, ``yes``,
    case-insensitive); anything else (unset, ``0``, ``false``) is off.
    """
    raw = os.environ.get("TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _run_job(job: _ScannerJob) -> tuple[str, list[ScanSignal], str]:
    label = f"{job.scanner.name}[{job.overlay}]" if job.overlay else job.scanner.name
    try:
        signals = job.scanner.scan()
        if job.overlay:
            signals = [
                ScanSignal(
                    kind=s.kind,
                    summary=s.summary,
                    payload={**s.payload, "overlay": job.overlay},
                )
                for s in signals
            ]
    except Exception as exc:
        logger.exception("Scanner %s raised", label)
        return label, [], f"{type(exc).__name__}: {exc}"
    return label, signals, ""


def _user_slack_id_for_overlay(overlay_name: str) -> str:
    """Resolve ``slack_user_id`` for the active overlay (overlay → global → empty).

    Used by :class:`ReviewNagScanner` to know where to DM long-stale MR
    warnings. Reads ``~/.teatree.toml`` directly so a fresh tick picks up
    a runtime config change without requiring an overlay reload.
    """
    try:
        toml_path = Path.home() / ".teatree.toml"
        if not toml_path.is_file():
            return ""
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    overlays = data.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    teatree_cfg = data.get("teatree") or {}
    return str(teatree_cfg.get("slack_user_id", ""))


def _user_identity_aliases_for_overlay(overlay_name: str) -> tuple[str, ...]:
    """Resolve ``user_identity_aliases`` honouring any per-overlay override.

    The active overlay's ``[overlays.<name>]`` table wins over the global
    ``[teatree]`` value; with no setting anywhere we return the empty
    tuple so the disposition scanner keeps its legacy behaviour.
    """
    try:
        global_value = tuple(load_config().user.user_identity_aliases)
        if overlay_name:
            for entry in discover_overlays():
                if entry.name == overlay_name:
                    override = entry.overrides.get("user_identity_aliases")
                    if override is not None:
                        return tuple(str(s) for s in override)
                    break
    except Exception:  # noqa: BLE001 — never break a tick on a config read.
        logger.warning("Failed to resolve user_identity_aliases for %r; defaulting to empty", overlay_name)
        return ()
    return global_value


def build_default_jobs(
    *,
    backends: list[OverlayBackends] | None = None,
    host: CodeHostBackend | None = None,
    messaging: MessagingBackend | None = None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[_ScannerJob]:
    """Build the default scanner jobs from one or more overlays.

    Pass *backends* to scan multiple overlays in one tick (each gets its
    own host/messaging credentials). The *host*/*messaging* shape
    is preserved for callers that resolve a single overlay themselves.
    """
    jobs: list[_ScannerJob] = [
        _ScannerJob(scanner=PendingTasksScanner(), overlay=""),
        _ScannerJob(scanner=IncomingEventsScanner(), overlay=""),
        _ScannerJob(scanner=OutboundAuditScanner(), overlay=""),
    ]

    if backends:
        for backend in backends:
            tag = backend.name
            if backend.external_db is not None:
                from teatree.loop.scanners.external_tickets import ExternalTicketsScanner  # noqa: PLC0415

                jobs.append(
                    _ScannerJob(
                        scanner=ExternalTicketsScanner(overlay_name=tag, db_path=backend.external_db),
                        overlay=tag,
                    ),
                )
            else:
                jobs.append(_ScannerJob(scanner=ActiveTicketsScanner(overlay_name=tag), overlay=tag))
            jobs.append(
                _ScannerJob(
                    scanner=StaleTicketsScanner(
                        overlay_name=tag,
                        threshold_days=backend.stale_threshold_days,
                    ),
                    overlay=tag,
                ),
            )
            # Multi-host: an overlay with both GitHub and GitLab PATs scans
            # both forges, so PRs on one platform don't drown out PRs on the
            # other (#976). The single-host path is preserved by iterating
            # ``backend.hosts`` (which is empty when no token resolved).
            jobs.extend(_jobs_for_backend_hosts(backend, tag))
            # #1136 / #1152 Periodic architectural-review scanner. CORE
            # always-on for every registered overlay — the cadence lives
            # in teatree-core config ([teatree] in ~/.teatree.toml) since
            # architectural review applies uniformly to all overlays'
            # worktrees, not as a per-overlay opt-in. Wired here (per
            # overlay, not per host) because the cadence is over the
            # overlay's ticket flow, not per-forge. Set
            # ``architectural_review_disabled = true`` per-overlay or
            # globally as an escape hatch.
            arch_scanner = _architectural_review_scanner_for(backend)
            if arch_scanner is not None:
                jobs.append(_ScannerJob(scanner=arch_scanner, overlay=tag))
            if backend.messaging is not None:
                jobs.extend(
                    [
                        # ``SlackMentionsScanner`` owns the JSONL drain and
                        # fans reaction events into the backend's reactions
                        # queue; ``SlackReviewIntentScanner`` must run after
                        # it so the queue is populated for the same tick.
                        _ScannerJob(scanner=SlackMentionsScanner(backend=backend.messaging), overlay=tag),
                        _ScannerJob(
                            scanner=SlackDmInboundScanner(backend=backend.messaging, overlay=tag),
                            overlay=tag,
                        ),
                        _ScannerJob(
                            scanner=SlackReviewIntentScanner(backend=backend.messaging, overlay=tag),
                            overlay=tag,
                        ),
                        _ScannerJob(
                            scanner=ReviewNagScanner(
                                messaging=backend.messaging,
                                user_slack_id=_user_slack_id_for_overlay(tag),
                            ),
                            overlay=tag,
                        ),
                    ],
                )
    else:
        if host is not None:
            jobs.extend(
                [
                    _ScannerJob(scanner=MyPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=ReviewerPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=AssignedIssuesScanner(host=host, ready_labels=ready_labels), overlay=""),
                ],
            )
        if messaging is not None:
            jobs.extend(
                [
                    _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=""),
                    _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=""), overlay=""),
                    _ScannerJob(scanner=SlackReviewIntentScanner(backend=messaging, overlay=""), overlay=""),
                ]
            )

    if notion_client is not None:
        jobs.append(_ScannerJob(scanner=NotionViewScanner(client=notion_client), overlay=""))
    return jobs


def build_default_scanners(
    *,
    host: CodeHostBackend | None,
    messaging: MessagingBackend | None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[Scanner]:
    """Single-overlay scanner builder kept for tests and ad-hoc CLI use."""
    return [
        job.scanner
        for job in build_default_jobs(
            host=host,
            messaging=messaging,
            notion_client=notion_client,
            ready_labels=ready_labels,
        )
    ]
