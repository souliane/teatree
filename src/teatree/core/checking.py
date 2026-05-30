"""Read-only "what did I miss" gather + render layer for ``/t3:checking`` (#1529).

Mirrors :mod:`teatree.core.standup`: a frozen-dataclass report built entirely
from existing rows, with ``to_dict()`` (JSON) and ``to_terse()`` (human)
renderers. Every query here is read-only — the gatherer never transitions a
:class:`Ticket` nor writes any row.

Three groups, each scoped to one overlay over the window ``[since, now)``:

* **Merged** — a :class:`MergeAudit` joined to its :class:`MergeClear`, merged
    inside the window. The clickable reference prefers an exact stored PR URL on
    ``Ticket.extra['pr_urls']`` and otherwise builds a host-aware URL from the
    slug + pr_id (never a bare id — a bare number is unclickable noise).
* **In-flight** — the latest :class:`TicketTransition` per ticket inside the
    window (the standup latest-per-ticket dedup), plus completed background
    :class:`TaskAttempt` runs.
* **Needs you** — pending :class:`DeferredQuestion` rows (NOT window-bounded: a
    pending question still needs the user however old it is) plus failed
    ``TaskAttempt`` runs inside the window. Failed agent runs are the durable
    proxy for "blocked" — core makes no live forge calls; an overlay opts into
    richer signals via :meth:`OverlayBase.get_checking_sources`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.merge_clear import MergeAudit, MergeClear
from teatree.core.models.task import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition

#: Per-group item cap; beyond it the renderer appends "…and X more".
DEFAULT_CAP = 5


class CheckItemDict(TypedDict):
    label: str
    url: str
    detail: str


class CheckGroupDict(TypedDict):
    title: str
    items: list[CheckItemDict]
    total: int


class CheckingReportDict(TypedDict):
    since: str
    merged: CheckGroupDict
    in_flight: CheckGroupDict
    needs_you: CheckGroupDict
    terse: str


@dataclass(frozen=True, slots=True)
class CheckItem:
    """One line in a group — a clickable reference plus optional detail.

    ``url`` is the clickable target (a PR/issue/ticket URL, or an actionable
    command for a deferred question); the renderer emits ``[label](url)`` so a
    reader never sees a bare numeric id.
    """

    label: str
    url: str
    detail: str = ""

    def to_dict(self) -> CheckItemDict:
        return CheckItemDict(label=self.label, url=self.url, detail=self.detail)

    def render(self) -> str:
        ref = f"[{self.label}]({self.url})" if self.url else self.label
        return f"  - {ref} — {self.detail}" if self.detail else f"  - {ref}"


@dataclass(frozen=True, slots=True)
class CheckGroup:
    """One titled group of items, with the full pre-cap total.

    ``total`` is the count before the cap (``total >= len(items)``) so the
    renderer can append "…and X more" without re-querying.
    """

    title: str
    items: list[CheckItem] = field(default_factory=list)
    total: int = 0

    def to_dict(self) -> CheckGroupDict:
        return CheckGroupDict(
            title=self.title,
            items=[item.to_dict() for item in self.items],
            total=self.total,
        )

    def render(self, *, cap: int = DEFAULT_CAP) -> list[str]:
        """Render the group header + capped items, or ``[]`` when empty.

        An empty group is omitted entirely (returns no lines). A group with
        more than *cap* items appends a ``…and X more`` line.
        """
        if self.total == 0:
            return []
        lines = [self.title]
        lines.extend(item.render() for item in self.items[:cap])
        if self.total > cap:
            lines.append(f"  …and {self.total - cap} more")
        return lines


@dataclass(frozen=True, slots=True)
class CheckingReport:
    """A terse, grouped, read-only "what did I miss" report for one window."""

    since: datetime
    merged: CheckGroup
    in_flight: CheckGroup
    needs_you: CheckGroup

    def to_dict(self) -> CheckingReportDict:
        return CheckingReportDict(
            since=self.since.isoformat(),
            merged=self.merged.to_dict(),
            in_flight=self.in_flight.to_dict(),
            needs_you=self.needs_you.to_dict(),
            terse=self.to_terse(),
        )

    def to_terse(self, *, overlay_name: str = "", cap: int = DEFAULT_CAP) -> str:
        """Render the answer-first terse view.

        Header line ``Since <local HH:MM> · <overlay>`` (the overlay suffix is
        dropped when empty), then the groups in fixed order. Empty groups are
        omitted. When every group is empty the whole report collapses to a
        single ``Nothing since <local time>.`` line — no preamble, no
        "Here is your report."
        """
        from django.utils import timezone  # noqa: PLC0415

        local = timezone.localtime(self.since) if timezone.is_aware(self.since) else self.since
        stamp = local.strftime("%H:%M")
        groups = [self.merged, self.in_flight, self.needs_you]
        if all(group.total == 0 for group in groups):
            return f"Nothing since {stamp}."
        header = f"Since {stamp} · {overlay_name}" if overlay_name else f"Since {stamp}"
        lines = [header]
        for group in groups:
            lines.extend(group.render(cap=cap))
        return "\n".join(lines)


def build_pr_url(*, slug: str, pr_id: int, code_host: str) -> str:
    """Build a clickable PR/MR web URL from a real ``owner/repo`` slug + id.

    GitLab uses ``/-/merge_requests/<id>``; GitHub (and the default) uses
    ``/pull/<id>``. Returns ``""`` unless *slug* is a genuine ``owner/repo``
    identifier (per :func:`merge_execution._looks_like_owner_repo`): a CLEAR's
    ``slug`` is a *workstream* slug (e.g. ``statusline-stale-wakeup``) or a
    branch name (``fix/foo``), never a repo, so emitting
    ``github.com/<workstream>/pull/<id>`` would be a wrong-host, unclickable
    link (#1559). A caller with no real repo slug falls back to the stored PR
    URL or the ticket's issue URL instead.
    """
    from teatree.core.merge_execution import _looks_like_owner_repo  # noqa: PLC0415

    clean = slug.strip().strip("/")
    if not _looks_like_owner_repo(clean):
        return ""
    if code_host.strip().lower() == "gitlab":
        return f"https://gitlab.com/{clean}/-/merge_requests/{pr_id}"
    return f"https://github.com/{clean}/pull/{pr_id}"


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


def gather_checking_report(  # noqa: PLR0913 — read-report entry-point; each kwarg is a documented window/scope input.
    *,
    since: datetime,
    now: datetime,
    overlay_name: str = "",
    code_host: str = "",
    overlay_repos: list[str] | None = None,
    cap: int = DEFAULT_CAP,
) -> CheckingReport:
    """Build a :class:`CheckingReport` for the window ``[since, now)``.

    Pure read path: aggregates ``MergeAudit`` / ``TicketTransition`` /
    ``TaskAttempt`` / ``DeferredQuestion`` rows, never mutating state. The
    window is half-open — ``merged_at``/``created_at``/``ended_at`` in
    ``[since, now)`` — and every group except pending questions is scoped to
    *overlay_name*.

    ``overlay_repos`` are the overlay's ``owner/repo`` (or bare ``repo``)
    identifiers used to scope a NULL-ticket ceremony merge to this overlay by
    its resolved repo — the ceremony ``ticket clear`` is normally issued
    without ``--ticket-id``, so a ticket-FK JOIN would silently drop it. The
    caller resolves the list from the active overlay; an empty list scopes the
    merged group to ticket-bearing CLEARs only (the back-compat behaviour).
    """
    scope = _MergedScope(overlay_name=overlay_name, code_host=code_host, overlay_repos=overlay_repos or [])
    return CheckingReport(
        since=since,
        merged=_merged_group(since=since, now=now, scope=scope, cap=cap),
        in_flight=_in_flight_group(since=since, now=now, overlay_name=overlay_name, cap=cap),
        needs_you=_needs_you_group(since=since, now=now, overlay_name=overlay_name, cap=cap),
    )


def _pr_url_for(ticket: Ticket | None, *, repo_slug: str, pr_id: int, code_host: str) -> str:
    """Prefer an exact stored PR URL, else a host-aware built URL, else issue URL.

    A stored ``extra['pr_urls']`` entry that mentions the pr_id is the exact
    forge web_url/html_url and wins. Otherwise the host-aware builder produces
    a URL from *repo_slug* — but only when it is a real ``owner/repo`` (a
    workstream/branch slug yields ``""`` from :func:`build_pr_url`, #1559). If
    no real repo is known, fall back to the ticket's issue URL so the reference
    is still clickable rather than a wrong-host link.
    """
    if ticket is not None:
        stored = (ticket.extra or {}).get("pr_urls") or []
        if isinstance(stored, list):
            for url in stored:
                if isinstance(url, str) and url and str(pr_id) in url:
                    return url
    built = build_pr_url(slug=repo_slug, pr_id=pr_id, code_host=code_host)
    if built:
        return built
    return ticket.issue_url if ticket is not None else ""


def _resolved_repo_slug(clear: MergeClear) -> str:
    """The real ``owner/repo`` for *clear*'s PR, or ``""`` when unresolvable.

    Wraps :func:`merge_execution.resolve_pr_repo_slug` (the same resolver the
    sanctioned merge keystone uses): an ``owner/repo``-shaped CLEAR slug as-is,
    else the ticket's ``issue_url`` repo, else the running clone's ``origin``.
    The resolver fails closed with :class:`MergePreconditionError` when nothing
    resolves; this read path swallows that into ``""`` so a catch-up report is
    never wedged by a single unresolvable row.
    """
    from teatree.core.merge_execution import MergePreconditionError, resolve_pr_repo_slug  # noqa: PLC0415

    try:
        return resolve_pr_repo_slug(clear)
    except MergePreconditionError:
        return ""


def _repo_in_overlay(repo_slug: str, overlay_repos: list[str]) -> bool:
    """True when *repo_slug* (a resolved ``owner/repo``) belongs to the overlay.

    An overlay declares its repos either as ``owner/repo`` (an exact match) or
    as a bare ``repo`` name (matching any owner, since a self-hosted namespace
    varies) — the same two shapes :mod:`teatree.loop.tick_resolvers` honours.
    """
    if not repo_slug:
        return False
    repo_name = repo_slug.rsplit("/", 1)[-1]
    for declared in overlay_repos:
        if not declared:
            continue
        if "/" in declared:
            if declared == repo_slug:
                return True
        elif declared == repo_name:
            return True
    return False


def _merged_group(*, since: datetime, now: datetime, scope: _MergedScope, cap: int) -> CheckGroup:
    qs = (
        MergeAudit.objects.filter(merged_at__gte=since, merged_at__lt=now)
        .select_related("clear", "clear__ticket")
        .order_by("-merged_at")
    )
    # Resolve each merge's real repo once (the resolver may read a git remote),
    # then scope and render from the cached slug — never re-resolving per field.
    scoped: list[tuple[MergeAudit, str]] = []
    for audit in qs:
        repo_slug = _resolved_repo_slug(audit.clear)
        if _audit_in_overlay(audit, repo_slug=repo_slug, scope=scope):
            scoped.append((audit, repo_slug))
    items = [
        CheckItem(
            label=f"{repo_slug or audit.clear.slug}#{audit.clear.pr_id}",
            url=_pr_url_for(
                audit.clear.ticket,
                repo_slug=repo_slug,
                pr_id=audit.clear.pr_id,
                code_host=scope.code_host,
            ),
            detail=(audit.clear.ticket.short_description if audit.clear.ticket else ""),
        )
        for audit, repo_slug in scoped[:cap]
    ]
    return CheckGroup(title="Merged", items=items, total=len(scoped))


def _audit_in_overlay(audit: MergeAudit, *, repo_slug: str, scope: _MergedScope) -> bool:
    """Whether *audit*'s merge belongs to the scoped overlay (#1559).

    An unscoped report (no ``overlay_name``) includes every merge. Otherwise a
    merge is in scope when EITHER its CLEAR's ticket is linked to this overlay
    (the precise back-compat path for ticket-bearing CLEARs) OR — the ceremony
    case — the CLEAR has no ticket and its already-resolved repo belongs to
    this overlay. A NULL-ticket CLEAR whose repo belongs to a *different*
    overlay is excluded; a blanket ``ticket IS NULL`` rule would over-report it.
    """
    if not scope.overlay_name:
        return True
    ticket = audit.clear.ticket
    if ticket is not None:
        return ticket.overlay == scope.overlay_name
    return _repo_in_overlay(repo_slug, scope.overlay_repos)


def _in_flight_group(*, since: datetime, now: datetime, overlay_name: str, cap: int) -> CheckGroup:
    transitions = (
        TicketTransition.objects.filter(created_at__gte=since, created_at__lt=now)
        .select_related("ticket")
        .order_by("ticket_id", "-created_at")
    )
    if overlay_name:
        transitions = transitions.filter(ticket__overlay=overlay_name)

    seen: set[int] = set()
    items: list[CheckItem] = []
    for tr in transitions:
        if tr.ticket_id in seen:
            continue
        seen.add(tr.ticket_id)
        ticket = tr.ticket
        items.append(
            CheckItem(
                label=f"#{ticket.ticket_number}",
                url=_ticket_url(ticket),
                detail=f"→ {tr.to_state}",
            ),
        )
    # Stable latest-first order: the queryset is ordered by ticket_id for the
    # dedup, so re-sort the deduped items by ticket number descending (newest
    # ticket first) before capping.
    items.sort(key=lambda item: int(item.label.lstrip("#")) if item.label.lstrip("#").isdigit() else 0, reverse=True)
    return CheckGroup(title="In-flight", items=items[:cap], total=len(items))


def _ticket_url(ticket: Ticket) -> str:
    """Clickable reference for a ticket: a PR URL for PR-bearing states, else issue URL."""
    if ticket.issue_url:
        return ticket.issue_url
    stored = (ticket.extra or {}).get("pr_urls") or []
    if isinstance(stored, list) and stored and isinstance(stored[-1], str):
        return stored[-1]
    return ""


def _needs_you_group(*, since: datetime, now: datetime, overlay_name: str, cap: int) -> CheckGroup:
    items: list[CheckItem] = []

    # Pending questions are NOT window-bounded — an old pending question still
    # needs the user. The id is a LOCAL DeferredQuestion handle, not an external
    # forge ref, so it must not render as a bare ``#NNN`` (that reads like an
    # unlinked issue and breaks the all-refs-clickable contract). Use the
    # bare-``#``-free ``Q<id>`` handle, and let the actionable answer command
    # carry the line's content — a command is an acceptable non-URL reference.
    overlay_slug = overlay_name or "<overlay>"
    for question in DeferredQuestion.pending():
        snippet = question.question.strip().replace("\n", " ")[:60]
        items.append(
            CheckItem(
                label=f"Q{question.pk}: {snippet}",
                url="",
                detail=f"t3 {overlay_slug} questions answer {question.pk} <text>",
            ),
        )

    # Failed agent runs inside the window are the durable "blocked" proxy.
    failed = (
        TaskAttempt.objects.filter(ended_at__gte=since, ended_at__lt=now)
        .filter(exit_code__gt=0)
        .select_related("task__ticket")
        .order_by("-ended_at")
    )
    if overlay_name:
        failed = failed.filter(task__ticket__overlay=overlay_name)
    seen_tickets: set[int] = set()
    for attempt in failed:
        ticket = attempt.task.ticket
        if ticket.pk in seen_tickets:
            continue
        seen_tickets.add(ticket.pk)
        items.append(
            CheckItem(
                label=f"#{ticket.ticket_number}",
                url=_ticket_url(ticket),
                detail="failed agent run",
            ),
        )

    return CheckGroup(title="Needs you", items=items[:cap], total=len(items))
