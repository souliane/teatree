"""Discover open, labelled issues and claim them for auto-implementation (#1553).

The always-on issue-implementer loop (default-OFF behind the
``issue_implementer_enabled`` gate) picks up issues that carry the
configured ``issue_implementer_label`` and are not already in flight. The
scanner is the discovery + claim half: it lists the user's open issues via
the code-host backend, keeps the ones carrying the label, and claims each
through the TOCTOU-safe :meth:`ImplementedIssueMarker.claim` so two
concurrent ticks never double-dispatch the same issue.

Whether the scanner runs at all is decided one layer up by
:func:`teatree.loop.scanner_factories._issue_implementer_scanner_for` — the triple
gate (enabled flag, in-flight concurrency budget, per-issue claim
idempotency). Dispatch routing of the emitted signals lands in C4 (#1554);
C3 stops at claim + signal emission.
"""

import logging
from dataclasses import dataclass, field
from typing import cast

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.fleet import wire
from teatree.core.models import NEEDS_TRIAGE_LABEL, ImplementedIssueMarker
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.forge_readback import existing_work_for_issue, fetch_open_prs, issue_number
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def _issue_labels(issue: RawAPIDict) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    out: list[str] = []
    for item in labels:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def _issue_is_open(issue: RawAPIDict) -> bool:
    """Treat an issue as open unless the backend explicitly reports otherwise.

    The forge issue-search the backends use already filters to open issues
    (``is:open``), so a missing ``state`` is open by construction; a present
    ``state`` of ``closed`` is the only thing that excludes an issue.
    """
    state = issue.get("state")
    return not (isinstance(state, str) and state.lower() == "closed")


@dataclass(slots=True)
class IssueImplementerScanner:
    """Claim open, labelled issues for the auto-implementer pipeline (#1553).

    Lists the configured *identities*' open issues on *host*, keeps the ones
    carrying *label* but NOT :data:`NEEDS_TRIAGE_LABEL` (a maintainer-applied
    hold), and claims each via the TOCTOU-safe
    :meth:`ImplementedIssueMarker.claim`. A claim that returns ``None`` (the
    row already exists — another tick or overlay took it) is skipped
    silently, so the scanner never double-dispatches. Each newly claimed
    issue surfaces one ``issue_implementer.claimed`` signal; the C4 dispatch
    layer routes those into the implementation pipeline.

    ``identities`` opts the scanner into a multi-alias union query (matching
    :class:`AssignedIssuesScanner`); empty falls back to
    ``host.current_user()``.

    ``readback_enabled`` (default on) runs the pre-dispatch forge read-back
    (:func:`~teatree.loop.scanners.forge_readback.existing_work_for_issue`)
    before each claim: an issue whose ``<ticket_number>-*`` branch or a
    referencing PR already exists on the forge is skipped, closing most of the
    cross-instance double-claim window that the local claim ledger cannot see.
    """

    host: CodeHostBackend
    label: str
    overlay_name: str = ""
    identities: tuple[str, ...] = field(default_factory=tuple)
    name: str = "issue_implementer"
    readback_enabled: bool = True
    #: When False (budget full, or no label) this tick only HEARTBEATS in-flight
    #: fleet claims and claims no new issue — the heartbeat must run even at full
    #: budget or an in-flight claim would expire mid-dispatch (fleet-safety Stage 2).
    can_claim: bool = True

    def scan(self) -> list[ScanSignal]:
        # Stage 2 B1: keep every in-flight claim un-stealable, on EVERY tick,
        # regardless of budget/label (self-gates to a no-op when the switch is off).
        wire.heartbeat_inflight_claims(self.overlay_name)
        if not self.can_claim:
            return []
        if not self.label:
            return []
        assignees = self._resolve_identities()
        if not assignees:
            return []
        candidates = self._candidate_issues(assignees)
        if not candidates:
            return []
        open_prs = fetch_open_prs(self.host, authors=assignees) if self.readback_enabled else []
        signals: list[ScanSignal] = []
        for issue in candidates:
            url = _issue_url(issue)
            try:
                hit = existing_work_for_issue(issue_url=url, ticket_number=issue_number(url), open_prs=open_prs)
                if hit is not None:
                    logger.info(
                        "IssueImplementerScanner read-back skip %s: %s (%s)",
                        url,
                        hit.reason,
                        hit.evidence_url,
                    )
                    continue
                marker = self._claim(url)
                if marker is None:
                    continue
                signals.append(
                    ScanSignal(
                        kind="issue_implementer.claimed",
                        summary=f"Claimed for auto-implement: {_issue_title(issue)}",
                        payload={
                            "url": url,
                            "raw": issue,
                            "overlay": self.overlay_name,
                        },
                    )
                )
            except Exception:
                logger.exception("IssueImplementerScanner failed on issue %s", url)
                continue
        return signals

    def _claim(self, url: str) -> ImplementedIssueMarker | None:
        """Claim *url*, cross-instance mutex first when the kill-switch is on.

        Kill-switch OFF (default): today's local ``get_or_create`` claim.
        ON: win the GitHub claim ref FIRST (fleet-safety Stage 2). ``None`` there —
        a live rival holds it, or the ref infra is unreachable and the acquire
        failed safe — skips this issue (do not dispatch). On a win the local marker
        is recorded as a CACHE of the ref, stamped with the fencing sha the ship
        gate re-verifies.
        """
        if not wire.fleet_claim_enabled(self.overlay_name):
            return ImplementedIssueMarker.objects.claim(url, overlay=self.overlay_name)
        claim = wire.acquire_issue_claim(url)
        if claim is None:
            return None
        return ImplementedIssueMarker.objects.cache_from_fleet_claim(
            url, self.overlay_name, claim_ref_sha=claim.sha, claimed_by_instance=claim.instance_id
        )

    def _candidate_issues(self, assignees: tuple[str, ...]) -> list[RawAPIDict]:
        """Open, URL-bearing issues carrying ``label`` but NOT :data:`NEEDS_TRIAGE_LABEL`.

        The claimable set, resolved before the per-issue read-back + claim loop
        so the forge open-PR fetch only fires when there is real work to guard.
        """
        candidates: list[RawAPIDict] = []
        for issue in self._collect_unique_issues(assignees):
            if not _issue_is_open(issue):
                continue
            labels = _issue_labels(issue)
            if self.label not in labels or NEEDS_TRIAGE_LABEL in labels:
                continue
            if not _issue_url(issue):
                continue
            candidates.append(issue)
        return candidates

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _collect_unique_issues(self, assignees: tuple[str, ...]) -> list[RawAPIDict]:
        """Union assigned issues across *assignees*, deduped by URL."""
        seen_urls: set[str] = set()
        issues: list[RawAPIDict] = []
        for assignee in assignees:
            for issue in self.host.list_assigned_issues(assignee=assignee):
                url = _issue_url(issue)
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                issues.append(issue)
        return issues
