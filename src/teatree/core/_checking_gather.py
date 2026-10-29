"""Private gatherer helpers for :mod:`teatree.core.checking`.

Separated from the public types module to stay within the 500-LOC limit.
All functions here are implementation details — callers import from
``teatree.core.checking``, not from this module directly.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import models
from django.db.models import F, OuterRef, Q, Subquery

from teatree.core.checking_types import CheckItem
from teatree.core.models.merge_clear import MergeAudit
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.task_cancel_source import operator_cancel_q
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition


def _latest_failed_tasks(now: datetime, task_scope: Q) -> dict[int, str]:
    latest_task_id = Task.objects.filter(ticket_id=OuterRef("ticket_id")).order_by("-pk").values("pk")[:1]
    latest_attempt_kind = (
        TaskAttempt.objects.filter(task_id=OuterRef("pk")).order_by("-ended_at", "-pk").values("failure_kind")[:1]
    )
    latest_attempt_end = (
        TaskAttempt.objects.filter(task_id=OuterRef("pk")).order_by("-ended_at", "-pk").values("ended_at")[:1]
    )
    cutoff = now - timedelta(days=7)
    failed = (
        Task.objects.filter(status=Task.Status.FAILED)
        .filter(task_scope)
        .annotate(
            _latest_task_id=Subquery(latest_task_id),
            _attempt_kind=Subquery(latest_attempt_kind),
            _attempt_end=Subquery(latest_attempt_end),
        )
        .filter(pk=F("_latest_task_id"))
        .filter(Q(_attempt_end__gte=cutoff) | Q(_attempt_end__isnull=True, created_at__gte=cutoff))
        .values_list("ticket_id", "failure_kind", "_attempt_kind")
    )
    return {ticket_id: kind or attempt_kind or "" for ticket_id, kind, attempt_kind in failed}


def _merge_safe_prs(prs: list[PullRequest]) -> set[tuple[str, int]]:
    heads: dict[tuple[str, int], str] = {}
    verdicts: dict[tuple[str, int], list[ReviewVerdict]] = {}
    passes: set[tuple[str, int]] = set()
    for verdict in (
        ReviewVerdict.objects.filter(pr_id__in=[int(pr.iid) for pr in prs if pr.iid.isdigit()])
        .order_by("-recorded_at", "-pk")
        .only("slug", "pr_id", "reviewed_sha", "verdict", "findings")
    ):
        key = verdict.slug.casefold(), verdict.pr_id
        head = verdict.reviewed_sha.strip().lower()
        if heads.setdefault(key, head) != head:
            continue
        verdicts.setdefault(key, []).append(verdict)
        if verdict.is_merge_safe():
            passes.add(key)
    return {
        key
        for key in passes
        if not ReviewVerdict.objects.unreconciled_holds_at(
            slug=key[0], pr_id=key[1], head_sha=heads[key], at_head=verdicts[key]
        )
    }


@dataclass(frozen=True, slots=True)
class _MergedScope:
    """How the merged group is scoped to one overlay (#1559).

    ``overlay_name`` is the ticket-FK back-compat scope for ticket-bearing
    CLEARs; ``overlay_repos`` (``owner/repo`` or bare ``repo``) is the
    resolved-repo scope for NULL-ticket ceremony CLEARs; ``code_host`` picks
    the PR URL shape.
    """

    overlay_name: str
    code_host: str
    overlay_repos: list[str]


def _url_matches_pr_id(url: str, pr_id: int) -> bool:
    """True when *url*'s last path segment equals *pr_id* (not just a substring)."""
    segment = url.rstrip("/").rsplit("/", 1)[-1]
    return segment == str(pr_id)


def pr_url_for(ticket: Ticket | None, *, repo_slug: str, pr_id: int, code_host: str) -> str:
    """Prefer an exact stored PR URL, else a host-aware built URL, else issue URL."""
    if ticket is not None:
        stored = (ticket.extra or {}).get("pr_urls") or []
        if isinstance(stored, list):
            for url in stored:
                if isinstance(url, str) and url and _url_matches_pr_id(url, pr_id):
                    return url
    from teatree.core.checking import build_pr_url  # noqa: PLC0415 — deferred: breaks _checking_gather ↔ checking cycle

    built = build_pr_url(slug=repo_slug, pr_id=pr_id, code_host=code_host)
    if built:
        return built
    return ticket.issue_url if ticket is not None else ""


def repo_entry_matches(declared: str, resolved_slug: str, *, overlay_owner: str | None) -> bool:
    """Whether a declared overlay repo matches a resolved ``owner/repo`` slug.

    A fully-qualified declared ``owner/repo`` matches only its exact slug. A
    bare declared repo name matches on repo segment, but only when the resolved
    slug's owner is allowed: a concrete *overlay_owner* requires the resolved
    owner to equal it — so a same-named repo under a different owner (declared
    ``acme-product`` vs resolved ``attacker-org/acme-product`` for an overlay
    owned by ``acme-org``) does NOT match. ``overlay_owner=None`` leaves the
    owner unconstrained (an overlay that declares no owner at all).
    """
    if not declared or not resolved_slug:
        return False
    if "/" in declared:
        return declared == resolved_slug
    resolved_owner, _, resolved_name = resolved_slug.rpartition("/")
    if declared != resolved_name:
        return False
    return overlay_owner is None or resolved_owner == overlay_owner


def _overlay_owners(overlay_repos: list[str]) -> set[str]:
    """The set of owners declared by the overlay's fully-qualified ``owner/repo`` entries."""
    return {declared.split("/", 1)[0] for declared in overlay_repos if declared and "/" in declared}


def repo_in_overlay(repo_slug: str, overlay_repos: list[str]) -> bool:
    """True when *repo_slug* (a resolved ``owner/repo``) belongs to the overlay.

    When the overlay declares any fully-qualified ``owner/repo``, a bare declared
    repo name is honoured only for resolved slugs whose owner is one of those
    declared owners — a same-named repo under a foreign owner cannot be claimed.
    An overlay that declares no owner at all keeps the lenient bare-name match.
    """
    if not repo_slug:
        return False
    owners = _overlay_owners(overlay_repos)
    resolved_owner = repo_slug.rpartition("/")[0]
    if owners and resolved_owner not in owners:
        # Constrained overlay, foreign owner: only an exact qualified entry can match.
        return any(declared == repo_slug for declared in overlay_repos if declared and "/" in declared)
    overlay_owner = resolved_owner if owners else None
    return any(
        repo_entry_matches(declared, repo_slug, overlay_owner=overlay_owner) for declared in overlay_repos if declared
    )


def ticket_url(ticket: Ticket) -> str:
    """Clickable reference for a ticket: a PR URL for PR-bearing states, else issue URL."""
    if ticket.issue_url:
        return ticket.issue_url
    stored = (ticket.extra or {}).get("pr_urls") or []
    if isinstance(stored, list) and stored and isinstance(stored[-1], str):
        return stored[-1]
    return ""


def audit_in_overlay(audit: MergeAudit, *, repo_slug: str, scope: _MergedScope) -> bool:
    """Whether *audit*'s merge belongs to the scoped overlay (#1559)."""
    if not scope.overlay_name:
        return True
    ticket = audit.clear.ticket
    if ticket is not None:
        return ticket.overlay == scope.overlay_name
    return repo_in_overlay(repo_slug, scope.overlay_repos)


def merged_entries_from_qs(
    *,
    since: datetime,
    now: datetime,
    scope: _MergedScope,
    overlay_tag: str = "",
) -> list[tuple[datetime, CheckItem]]:
    """Every scoped merge in the window as ``(merged_at, item)``, newest first.

    The timestamp travels with the item so a caller spanning several overlays can
    order the union before capping it — capping each overlay's slice first let one
    overlay's older merges displace another's newer ones.
    """
    from teatree.core.merge import resolved_repo_slug  # noqa: PLC0415 — deferred merge edge

    qs = (
        MergeAudit.objects.filter(merged_at__gte=since, merged_at__lt=now)
        .select_related("clear", "clear__ticket")
        .order_by("-merged_at")
    )
    entries: list[tuple[datetime, CheckItem]] = []
    for audit in qs:
        repo_slug = resolved_repo_slug(audit.clear)
        if not audit_in_overlay(audit, repo_slug=repo_slug, scope=scope):
            continue
        ticket = audit.clear.ticket
        raw_detail = ticket.short_description if ticket else ""
        detail = (f"{raw_detail} {overlay_tag}".strip() if raw_detail else overlay_tag) if overlay_tag else raw_detail
        entries.append(
            (
                audit.merged_at,
                CheckItem(
                    label=f"{repo_slug or audit.clear.slug}#{audit.clear.pr_id}",
                    url=pr_url_for(ticket, repo_slug=repo_slug, pr_id=audit.clear.pr_id, code_host=scope.code_host),
                    detail=detail,
                ),
            )
        )
    return entries


def merged_group_from_qs(
    *,
    since: datetime,
    now: datetime,
    scope: _MergedScope,
    cap: int,
    overlay_tag: str = "",
) -> tuple[list, int]:
    """Query and scope the merged audits; return (items, total)."""
    entries = merged_entries_from_qs(since=since, now=now, scope=scope, overlay_tag=overlay_tag)
    return [item for _, item in entries[:cap]], len(entries)


def motion_for_overlay(
    *,
    window: tuple[datetime, datetime],
    overlay_name: str,
    overlay_tag: str,
    seen_in_flight: set[int],
    seen_failed: set[int],
) -> tuple[list, list]:
    """Query ticket transitions and failed attempts for one overlay."""
    since, now = window
    in_flight: list = []
    failed: list = []

    transitions = (
        TicketTransition.objects.filter(created_at__gte=since, created_at__lt=now)
        .filter(ticket__overlay=overlay_name)
        .select_related("ticket")
        .order_by("ticket_id", "-created_at")
    )
    seen_local: set[int] = set()
    for tr in transitions:
        if tr.ticket_id in seen_local:
            continue
        seen_local.add(tr.ticket_id)
        tick = tr.ticket
        if tick.pk in seen_in_flight:
            continue
        seen_in_flight.add(tick.pk)
        in_flight.append(
            CheckItem(
                label=f"#{tick.ticket_number}",
                url=ticket_url(tick),
                detail=f"→ {tr.to_state} {overlay_tag}",
                title=tick.short_description,
            )
        )

    attempts = failed_attempts(since=since, now=now, overlay_name=overlay_name)
    for attempt in attempts:
        tick = attempt.task.ticket
        if tick.pk in seen_failed:
            continue
        seen_failed.add(tick.pk)
        failed.append(
            CheckItem(
                label=f"#{tick.ticket_number}",
                url=ticket_url(tick),
                detail=f"failed agent run {overlay_tag}",
                title=tick.short_description,
            )
        )

    return in_flight, failed


def failed_attempts(*, since: datetime, now: datetime, overlay_name: str) -> models.QuerySet[TaskAttempt]:
    attempts = (
        TaskAttempt.objects.filter(ended_at__gte=since, ended_at__lt=now)
        .filter(outcome__in=TaskAttempt.FAILED_OUTCOMES)
        .exclude(operator_cancel_q())
        .select_related("task__ticket")
        .order_by("-ended_at")
    )
    return attempts.filter(task__ticket__overlay=overlay_name) if overlay_name else attempts
