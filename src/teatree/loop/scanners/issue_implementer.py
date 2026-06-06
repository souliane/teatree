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

from teatree.backends.protocols import CodeHostBackend
from teatree.core.models import NEEDS_TRIAGE_LABEL, ImplementedIssueMarker
from teatree.loop.scanners.base import ScanSignal
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
    """

    host: CodeHostBackend
    label: str
    overlay_name: str = ""
    identities: tuple[str, ...] = field(default_factory=tuple)
    name: str = "issue_implementer"

    def scan(self) -> list[ScanSignal]:
        if not self.label:
            return []
        assignees = self._resolve_identities()
        if not assignees:
            return []
        signals: list[ScanSignal] = []
        for issue in self._collect_unique_issues(assignees):
            url = _issue_url(issue) or "<unknown>"
            try:
                if not _issue_is_open(issue):
                    continue
                labels = _issue_labels(issue)
                if self.label not in labels:
                    continue
                if NEEDS_TRIAGE_LABEL in labels:
                    continue
                url = _issue_url(issue)
                if not url:
                    continue
                marker = ImplementedIssueMarker.objects.claim(url, overlay=self.overlay_name)
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
