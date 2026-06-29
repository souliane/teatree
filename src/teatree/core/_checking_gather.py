"""Private gatherer helpers for :mod:`teatree.core.checking`.

Separated from the public types module to stay within the 500-LOC limit.
All functions here are implementation details — callers import from
``teatree.core.checking``, not from this module directly.
"""

from dataclasses import dataclass
from datetime import datetime

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.merge_clear import MergeAudit, MergeClear
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition


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
    from teatree.core.checking import build_pr_url  # noqa: PLC0415

    built = build_pr_url(slug=repo_slug, pr_id=pr_id, code_host=code_host)
    if built:
        return built
    return ticket.issue_url if ticket is not None else ""


def resolved_repo_slug(clear: MergeClear) -> str:
    """The real ``owner/repo`` for *clear*'s PR, or ``""`` when unresolvable."""
    from teatree.core.merge import MergePreconditionError, resolve_pr_repo_slug  # noqa: PLC0415

    try:
        return resolve_pr_repo_slug(clear)
    except MergePreconditionError:
        return ""


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


def merged_group_from_qs(
    *,
    since: datetime,
    now: datetime,
    scope: _MergedScope,
    cap: int,
    overlay_tag: str = "",
) -> tuple[list, int]:
    """Query and scope the merged audits; return (items, total)."""
    from teatree.core.checking import CheckItem  # noqa: PLC0415

    qs = (
        MergeAudit.objects.filter(merged_at__gte=since, merged_at__lt=now)
        .select_related("clear", "clear__ticket")
        .order_by("-merged_at")
    )
    scoped: list[tuple[MergeAudit, str]] = []
    for audit in qs:
        repo_slug = resolved_repo_slug(audit.clear)
        if audit_in_overlay(audit, repo_slug=repo_slug, scope=scope):
            scoped.append((audit, repo_slug))

    items = []
    for audit, repo_slug in scoped[:cap]:
        ticket = audit.clear.ticket
        raw_detail = ticket.short_description if ticket else ""
        detail = (f"{raw_detail} {overlay_tag}".strip() if raw_detail else overlay_tag) if overlay_tag else raw_detail
        items.append(
            CheckItem(
                label=f"{repo_slug or audit.clear.slug}#{audit.clear.pr_id}",
                url=pr_url_for(ticket, repo_slug=repo_slug, pr_id=audit.clear.pr_id, code_host=scope.code_host),
                detail=detail,
            )
        )
    return items, len(scoped)


def motion_for_overlay(
    *,
    window: tuple[datetime, datetime],
    overlay_name: str,
    overlay_tag: str,
    seen_in_flight: set[int],
    seen_failed: set[int],
) -> tuple[list, list]:
    """Query ticket transitions and failed attempts for one overlay."""
    from teatree.core.checking import CheckItem  # noqa: PLC0415

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

    attempts = (
        TaskAttempt.objects.filter(ended_at__gte=since, ended_at__lt=now)
        .filter(exit_code__gt=0)
        .filter(task__ticket__overlay=overlay_name)
        .select_related("task__ticket")
        .order_by("-ended_at")
    )
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


def deferred_questions(*, overlay_slug: str) -> list:
    """Return pending :class:`DeferredQuestion` rows as :class:`CheckItem` list."""
    from teatree.core.checking import CheckItem  # noqa: PLC0415

    items: list = []
    for question in DeferredQuestion.pending():
        snippet = question.question.strip().replace("\n", " ")[:60]
        items.append(
            CheckItem(
                label=f"Q{question.pk}: {snippet}",
                url="",
                detail=f"t3 {overlay_slug} questions answer {question.pk} <text>",
            )
        )
    return items
