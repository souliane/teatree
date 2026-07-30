"""Poll ``REVIEW_REQUESTED`` PRs for forge approval — revives the M7 lane (#8).

No production path ever transitioned a :class:`~teatree.core.models.pull_request.PullRequest`
to ``APPROVED``, so the whole merge_authorization waiting lane — the waiting-digest
DM, the statusline count, and the #961 approval check-mark reaction — was
structurally cold. This scanner is that missing producer: every tick it reads each
``REVIEW_REQUESTED``, unmerged PR row's live forge approval (GitHub
``reviewDecision`` / GitLab approvals endpoint) via :func:`sync_forge_approvals`
(co-located below) and drives an approved one to ``APPROVED``.

Outbound-gating floor: the ``approve`` transition fires the #961 approval
check-mark reaction, a colleague-visible Slack post routed through the on-behalf
gate. At default settings (``on_behalf_post_mode = draft_or_ask``) the reaction is
SKIPPED, so this revived lane sends nothing unsanctioned — only the FSM state
change and the best-effort bot→user waiting-digest self-DM (posted by the global
``WaitingDigestScanner``) happen at default. The forge approval read is a plain
GET, never a write.

The scanner resolves its code host from its overlay (:func:`code_host_from_overlay`)
so it can be registered through the shared single-overlay messaging builder
(:func:`~teatree.loop.domain_jobs._inbound_messaging_jobs`) without a per-host
fan-out; an explicit ``host`` may be injected for tests.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models import PullRequest

logger = logging.getLogger(__name__)


def sync_forge_approvals(host: "CodeHostBackend", prs: "Iterable[PullRequest]") -> "list[PullRequest]":
    """Drive each forge-approved ``REVIEW_REQUESTED`` PR to ``APPROVED`` (#8).

    The first production path that ever transitions a
    :class:`~teatree.core.models.pull_request.PullRequest` to ``APPROVED`` —
    reviving the M7 merge_authorization waiting lane that was cold because
    nothing wrote that state. For each unmerged ``REVIEW_REQUESTED`` row it
    reads the live forge approval (``host.get_mr_approvals``: GitHub
    ``reviewDecision`` / GitLab approvals endpoint) and calls ``row.approve()``
    when the forge threshold is satisfied.

    The ``approve`` transition fires the #961 approval check-mark reaction, a
    colleague-visible Slack post routed through the on-behalf gate — at default
    settings it is SKIPPED, so this revival sends nothing unsanctioned. Per-row
    isolation: an auth failure, a deleted PR, or a refused transition is logged
    and skipped so one bad row never aborts the others. Returns the rows newly
    transitioned to ``APPROVED``.
    """
    from teatree.core.models import PullRequest  # noqa: PLC0415 — deferred: Django-backed read

    approved: list[PullRequest] = []
    for row in prs:
        if row.state != PullRequest.State.REVIEW_REQUESTED:
            continue
        try:
            pr_iid = int(row.iid)
        except (TypeError, ValueError):
            continue
        try:
            state = host.get_mr_approvals(repo=row.repo, pr_iid=pr_iid)
            if state["approvals_left"] > 0:
                continue
            row.approve()
            row.save()
        except Exception:
            logger.exception("forge-approval sync failed for %s — skipping", row.url)
            continue
        approved.append(row)
    return approved


@dataclass(slots=True)
class PrApprovalScanner:
    """Emit ``pr.approved`` and drive forge-approved review-requested PRs to ``APPROVED``.

    ``overlay`` scopes which PR rows are polled (an empty overlay is the
    single-overlay default); ``host`` is resolved from the overlay at scan time
    unless injected.
    """

    overlay: str = ""
    host: "CodeHostBackend | None" = field(default=None)
    name: str = "pr_approvals"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models import PullRequest  # noqa: PLC0415 — deferred: Django-backed read

        host = self.host or self._resolve_host()
        if host is None:
            return []
        rows = list(PullRequest.objects.filter(state=PullRequest.State.REVIEW_REQUESTED).select_related("ticket"))
        if self.overlay:
            rows = [row for row in rows if row.overlay in {self.overlay, ""}]
        if not rows:
            return []
        return [
            ScanSignal(
                kind="pr.approved",
                summary=f"forge-approved: {row.repo}#{row.iid}",
                payload={"url": row.url, "repo": row.repo, "iid": row.iid, "overlay": row.overlay},
            )
            for row in sync_forge_approvals(host, rows)
        ]

    def _resolve_host(self) -> "CodeHostBackend | None":
        from teatree.core.backend_factory import code_host_from_overlay  # noqa: PLC0415 — needs django.setup()

        try:
            return code_host_from_overlay(self.overlay or None)
        except Exception:
            logger.exception("PrApprovalScanner: could not resolve a code host for overlay %r", self.overlay)
            return None
