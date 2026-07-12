"""Reconcile manually-opened MRs into ``PullRequest`` rows on each tick (#1912).

A PR opened OUTSIDE the ship pipeline — a manual ``gh pr create``, an MR opened
in another overlay — has no :class:`~teatree.core.models.pull_request.PullRequest`
row, so it never enters review-request tracking or the FSM. The loop's
:class:`~teatree.loop.scanners.my_prs.MyPrsScanner` already surfaces every open PR
each tick; this consolidator projects those ``my_pr.*`` signals into rows for the
PRs that resolve to a ticket — reusing the close-keyword footer parse and
``Ticket.extra["prs"]`` fallbacks the statusline index
(:mod:`teatree.loop.pr_ticket_index`) already uses.

A PR with no resolvable ticket stays statusline-only (``PullRequest.ticket`` is
non-null by design). New rows enter ``open``; a live-merged PR transitions via
``mark_merged``. Rows are indistinguishable from ship-pipeline rows, so
review-request tracking and the FSM need no special-casing.

This mirrors :mod:`teatree.loop.open_prs`: the tick is the single fetch point and
the reconciler reuses the scan data it already produced — zero extra code-host
calls. A persisted row's ``create_verification`` is stamped ``CONFIRMED`` (#1194):
the ``my_pr.*`` scan that produced it is a live-forge read, so the row's existence
is re-read-confirmed by construction; the phantom-URL case is caught upstream on
the ship/ensure create path.
"""

import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from teatree.loop.pr_ticket_index import _description_from_payload, resolve_author_ticket
from teatree.utils.close_keywords import parse_closes_ticket
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models import Ticket
    from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

# The open-PR signal kinds MyPrsScanner emits (mirrors open_prs._OPEN_PR_SIGNAL_KINDS).
_MY_PR_SIGNAL_KINDS = frozenset({"my_pr.open", "my_pr.draft_notes", "my_pr.failed"})


@dataclass(frozen=True, slots=True)
class _ScannedPr:
    url: str
    iid: int
    slug: str
    description: str
    merged: bool


def _is_merged(raw: Mapping[str, Any]) -> bool:
    """Read the merged flag across host shapes (GitHub ``merged``, GitLab ``state``)."""
    if raw.get("merged") is True:
        return True
    if raw.get("state") == "merged":
        return True
    return bool(raw.get("merged_at"))


def _scanned_prs(signals: "list[ScanSignal]") -> list[_ScannedPr]:
    """Project the tick's ``my_pr.*`` signals into reconciliation inputs, deduped by URL."""
    seen: set[str] = set()
    prs: list[_ScannedPr] = []
    for signal in signals:
        if signal.kind not in _MY_PR_SIGNAL_KINDS:
            continue
        payload = signal.payload
        url = payload.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        ref = pr_ref_from_url(url)
        raw = payload.get("raw")
        raw_dict: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        iid = payload.get("iid")
        prs.append(
            _ScannedPr(
                url=url,
                iid=iid if isinstance(iid, int) and iid > 0 else (ref.pr_id if ref else 0),
                slug=ref.slug if ref else "",
                description=_description_from_payload(payload),
                merged=_is_merged(raw_dict),
            )
        )
    return prs


def _resolve_ticket(pr: _ScannedPr) -> "Ticket | None":
    """Resolve the ticket a scanned PR belongs to, or ``None`` (footerless stays statusline-only).

    Footer first: a ``Closes/Fixes #N`` keyword parsed from the PR body resolves
    against the collision-free ``<slug>#N`` repo-namespaced key so issue #N of one
    repo never binds a PR from another. Falls back to the FK + ``Ticket.extra["prs"]``
    walk :func:`resolve_author_ticket` already implements for a bare manual MR.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    number = parse_closes_ticket(pr.description)
    if number and pr.slug:
        with suppress(Ticket.DoesNotExist):
            return Ticket.objects.resolve(f"{pr.slug}#{number}")
    return resolve_author_ticket(slug=pr.slug, pr_id=pr.iid, pr_url=pr.url)


def _reconcile_one(pr: _ScannedPr) -> bool:
    """Upsert one row; return True when a row was created or transitioned to merged."""
    from teatree.core.models import PullRequest  # noqa: PLC0415 — deferred: ORM import needs the app registry

    row = PullRequest.objects.filter(url=pr.url).first()
    if row is None:
        ticket = _resolve_ticket(pr)
        if ticket is None:
            return False
        # The `my_pr.*` scan that produced `pr` is a live-forge read that returned
        # this PR, so its existence is verify-by-re-read confirmed (#1194): a
        # persisted row is stamped CONFIRMED. The ship/ensure create path catches
        # a phantom URL (a 404 re-read) before it can ever reach this reconciler.
        row, changed = PullRequest.objects.get_or_create(
            url=pr.url,
            defaults={
                "ticket": ticket,
                "overlay": ticket.overlay,
                "repo": pr.slug,
                "iid": str(pr.iid),
                "create_verification": PullRequest.CreateVerification.CONFIRMED,
                "create_verified_at": timezone.now(),
            },
        )
    else:
        changed = False
    if pr.merged and row.state != PullRequest.State.MERGED:
        row.mark_merged()
        row.save()
        changed = True
    return changed


def reconcile_manual_prs(signals: "list[ScanSignal]") -> int:
    """Upsert a ``PullRequest`` row per open-PR signal that resolves to a ticket.

    Returns the number of rows created or transitioned to merged. Idempotent —
    re-running over the same signals changes nothing (rows are unique on URL).
    Per-row isolation: a single bad row is logged and skipped so it can never
    abort the reconciliation of the others.
    """
    count = 0
    for pr in _scanned_prs(signals):
        try:
            if _reconcile_one(pr):
                count += 1
        except Exception:
            logger.exception("manual-PR reconcile failed for %s — skipping", pr.url)
    return count
