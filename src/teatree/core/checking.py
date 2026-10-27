"""Read-only "what did I miss" gather + render layer for ``/t3:checking`` (#1529).

Mirrors :mod:`teatree.core.standup`: a frozen-dataclass report built entirely
from existing rows, with ``to_dict()`` (JSON) and ``to_terse()`` (human)
renderers. Every query here is read-only — the gatherer never transitions a
:class:`Ticket` nor writes any row.

Four delta groups scoped to the window ``[since, now)`` and one live group:

* **Merged** — a :class:`MergeAudit` joined to its :class:`MergeClear`, merged
    inside the window. The clickable reference prefers an exact stored PR URL on
    ``Ticket.extra['pr_urls']`` and otherwise builds a host-aware URL from the
    slug + pr_id (never a bare id — a bare number is unclickable noise).
* **In-flight** — the latest :class:`TicketTransition` per ticket inside the
    window (the standup latest-per-ticket dedup), plus completed background
    :class:`TaskAttempt` runs.
* **Needs you** — all four live waiting kinds (NOT window-bounded: a
    pending wait still needs the user however old it is) plus failed
    ``TaskAttempt`` runs inside the window. Failed agent runs are the durable
    proxy for "blocked" — core makes no live forge calls; an overlay opts into
    richer signals via :meth:`OverlayBase.get_checking_sources`.
* **Metered** — one row per ticket with metered attempts inside the window,
    showing the attempt count and estimated total cost.
* **Remaining** — live work, queue, holds, and measured ETA ranges; independent
    of the delta window and excluded from recap notifications.

Multi-overlay aggregation (``gather_all_overlays_report``):

:func:`gather_all_overlays_report` merges per-overlay groups into a single
:class:`AllOverlaysReport`. Each overlay-scoped item carries an ``[overlay]``
inline tag in its detail so the reader can tell provenance. The global owner
waiting gather runs exactly once, so a pending question never appears twice.
"""

import operator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypedDict

from django.db.models import Count, Max, Sum

from teatree.core import checking_types
from teatree.core._checking_gather import (
    _MergedScope,
    failed_attempts,
    merged_entries_from_qs,
    merged_group_from_qs,
    motion_for_overlay,
    ticket_url,
)
from teatree.core.checking_remaining import empty_remaining, gather_remaining
from teatree.core.checking_types import (
    DEFAULT_CAP,
    CheckGroup,
    CheckGroupDict,
    CheckItem,
    OwnerWaitItem,
    RemainingGroup,
    RemainingGroupDict,
)
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.waiting import WaitingKind, format_age, gather_waiting

CheckItemDict = checking_types.CheckItemDict


def owner_wait_items(*, overlay_slug: str, overlay: str = "") -> list[OwnerWaitItem]:
    waits = sorted(gather_waiting(overlay), key=lambda entry: entry.age, reverse=True)
    items: list[OwnerWaitItem] = []
    for wait in waits:
        question = wait.kind == WaitingKind.QUESTION
        label = f"Q{wait.row_id}: {wait.ref}" if question else wait.ref
        if not overlay and wait.overlay:
            label = f"[{wait.overlay}] {label}"
        detail = (
            "awaiting your Slack reply"
            if question and wait.owner_only
            else (
                f"answer in its Slack thread, or t3 {overlay_slug} questions dismiss {wait.row_id}"
                " --reason '<evidence>'"
            )
            if question
            else f"waiting {format_age(wait.age)}"
        )
        items.append(
            OwnerWaitItem(
                label=label,
                url=wait.url,
                detail=detail,
                kind=wait.kind,
                age_days=max(0, wait.age.days),
                row_id=wait.row_id,
                owner_only=wait.owner_only,
            )
        )
    return items


def internal_escalation_count() -> int:
    return DeferredQuestion.internal_pending().count()


class CheckingReportDict(TypedDict):
    since: str
    merged: CheckGroupDict
    in_flight: CheckGroupDict
    needs_you: CheckGroupDict
    metered: CheckGroupDict
    internal_escalations: int
    remaining: RemainingGroupDict
    terse: str


class AllOverlaysReportDict(TypedDict):
    all_overlays: list[str]
    earliest_since: str
    merged: CheckGroupDict
    in_flight: CheckGroupDict
    needs_you: CheckGroupDict
    metered: CheckGroupDict
    internal_escalations: int
    remaining: RemainingGroupDict
    terse: str


@dataclass(frozen=True, slots=True)
class CheckingReport:
    """A terse, grouped, read-only "what did I miss" report for one window."""

    since: datetime
    merged: CheckGroup
    in_flight: CheckGroup
    needs_you: CheckGroup
    metered: CheckGroup
    internal_escalations: int = 0
    remaining: RemainingGroup = field(default_factory=empty_remaining)

    @property
    def groups(self) -> list[CheckGroup]:
        return [*self.recap_groups, self.remaining]

    @property
    def recap_groups(self) -> list[CheckGroup]:
        return [self.merged, self.in_flight, self.needs_you, self.metered]

    def to_dict(self) -> CheckingReportDict:
        return CheckingReportDict(
            since=self.since.isoformat(),
            merged=self.merged.to_dict(),
            in_flight=self.in_flight.to_dict(),
            needs_you=self.needs_you.to_dict(),
            metered=self.metered.to_dict(),
            internal_escalations=self.internal_escalations,
            remaining=self.remaining.to_dict(),
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
        from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

        local = timezone.localtime(self.since) if timezone.is_aware(self.since) else self.since
        stamp = local.strftime("%H:%M")
        groups = self.groups
        if all(not group.has_table_content() for group in groups) and not self.internal_escalations:
            return f"Nothing since {stamp}."
        header = f"Since {stamp} · {overlay_name}" if overlay_name else f"Since {stamp}"
        lines = [header]
        for group in groups:
            lines.extend(group.render(cap=cap))
        if self.internal_escalations:
            lines.append(f"{self.internal_escalations} internal escalations: factory work, not questions")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class AllOverlaysReport:
    """Aggregated "what did I miss" report spanning all configured overlays.

    ``earliest_since`` is the oldest window start across all overlays —
    the header stamps from that so the user sees the widest window covered.
    Items in overlay-scoped groups carry an ``[overlay]`` tag in their detail.
    ``DeferredQuestion`` items are global (no overlay tag).
    """

    all_overlays: list[str]
    earliest_since: datetime
    merged: CheckGroup
    in_flight: CheckGroup
    needs_you: CheckGroup
    metered: CheckGroup
    internal_escalations: int = 0
    remaining: RemainingGroup = field(default_factory=empty_remaining)

    @property
    def groups(self) -> list[CheckGroup]:
        return [*self.recap_groups, self.remaining]

    @property
    def recap_groups(self) -> list[CheckGroup]:
        return [self.merged, self.in_flight, self.needs_you, self.metered]

    def to_dict(self) -> AllOverlaysReportDict:
        return AllOverlaysReportDict(
            all_overlays=self.all_overlays,
            earliest_since=self.earliest_since.isoformat(),
            merged=self.merged.to_dict(),
            in_flight=self.in_flight.to_dict(),
            needs_you=self.needs_you.to_dict(),
            metered=self.metered.to_dict(),
            internal_escalations=self.internal_escalations,
            remaining=self.remaining.to_dict(),
            terse=self.to_terse(),
        )

    def to_terse(self, *, cap: int = DEFAULT_CAP) -> str:
        """Render the terse view for all overlays.

        Header: ``Since <local HH:MM> · all overlays``.
        Empty report: ``Nothing since <local time>.``
        """
        from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

        local = (
            timezone.localtime(self.earliest_since) if timezone.is_aware(self.earliest_since) else self.earliest_since
        )
        stamp = local.strftime("%H:%M")
        groups = self.groups
        if all(not group.has_table_content() for group in groups) and not self.internal_escalations:
            return f"Nothing since {stamp}."
        lines = [f"Since {stamp} · all overlays"]
        for group in groups:
            lines.extend(group.render(cap=cap))
        if self.internal_escalations:
            lines.append(f"{self.internal_escalations} internal escalations: factory work, not questions")
        return "\n".join(lines)


def build_pr_url(*, slug: str, pr_id: int, code_host: str) -> str:
    """Build a clickable PR/MR web URL from a real ``owner/repo`` slug + id.

    GitLab uses ``/-/merge_requests/<id>``; GitHub (and the default) uses
    ``/pull/<id>``. Returns ``""`` unless *slug* is a genuine ``owner/repo``
    identifier (per :func:`merge.pr_slug_resolution._looks_like_owner_repo`): a CLEAR's
    ``slug`` is a *workstream* slug (e.g. ``statusline-stale-wakeup``) or a
    branch name (``fix/foo``), never a repo, so emitting
    ``github.com/<workstream>/pull/<id>`` would be a wrong-host, unclickable
    link (#1559). A caller with no real repo slug falls back to the stored PR
    URL or the ticket's issue URL instead.
    """
    from teatree.core.merge import _looks_like_owner_repo  # noqa: PLC0415 — deferred: call-time import, kept lazy

    clean = slug.strip().strip("/")
    if not _looks_like_owner_repo(clean):
        return ""
    if code_host.strip().lower() == "gitlab":
        return f"https://gitlab.com/{clean}/-/merge_requests/{pr_id}"
    return f"https://github.com/{clean}/pull/{pr_id}"


def gather_all_overlays_report(
    *,
    overlay_windows: dict[str, tuple[datetime, datetime]],
    overlay_configs: dict[str, tuple[str, list[str]]],
    cap: int = DEFAULT_CAP,
) -> AllOverlaysReport:
    """Build an :class:`AllOverlaysReport` spanning every overlay in *overlay_windows*.

    *overlay_windows* maps overlay name → ``(since, now)`` pair.
    *overlay_configs* maps overlay name → ``(code_host, overlay_repos)`` pair.

    The global waiting gather runs exactly ONCE — pending questions are not
    scoped per overlay, so repeating the gather would duplicate them.

    Overlay-scoped items (merged, in-flight, failed attempts) have their
    detail text suffixed with ``[overlay]`` so the reader sees provenance.
    """
    merged_entries, in_flight_items, failed_items, earliest_since = _accumulate_overlays(
        overlay_windows=overlay_windows, overlay_configs=overlay_configs
    )
    in_flight_items.sort(
        key=lambda item: int(item.label.lstrip("#")) if item.label.lstrip("#").isdigit() else 0,
        reverse=True,
    )
    overlay_slug = next(iter(overlay_windows), "") or "<overlay>"
    needs_you_items: list[CheckItem] = [*owner_wait_items(overlay_slug=overlay_slug)]
    needs_you_items.extend(failed_items)
    effective_since = earliest_since or next(iter(overlay_windows.values()), (None, None))[0] or datetime.now(UTC)
    metered_groups = [
        _metered_entries(since=since, now=now, overlay_name=name, overlay_tag=f"[{name}]", cap=cap)
        for name, (since, now) in overlay_windows.items()
    ]
    metered_entries = [entry for entries, _ in metered_groups for entry in entries]
    metered_entries.sort(key=operator.itemgetter(0), reverse=True)
    return AllOverlaysReport(
        all_overlays=list(overlay_windows),
        earliest_since=effective_since,
        merged=CheckGroup(
            title="Merged",
            items=[item for _, item in merged_entries[:cap]],
            total=len(merged_entries),
        ),
        in_flight=CheckGroup(title="In-flight", items=in_flight_items[:cap], total=len(in_flight_items)),
        needs_you=CheckGroup(title="Needs you", items=needs_you_items[:cap], total=len(needs_you_items)),
        internal_escalations=internal_escalation_count(),
        metered=CheckGroup(
            title="Metered",
            items=[item for _, item in metered_entries[:cap]],
            total=sum(total for _, total in metered_groups),
        ),
        remaining=gather_remaining(
            now=max((window[1] for window in overlay_windows.values()), default=effective_since),
            overlay_names=list(overlay_windows),
            tag_overlays=True,
            cap=cap,
        ),
    )


def _accumulate_overlays(
    *,
    overlay_windows: dict[str, tuple[datetime, datetime]],
    overlay_configs: dict[str, tuple[str, list[str]]],
) -> tuple[list[tuple[datetime, CheckItem]], list[CheckItem], list[CheckItem], datetime | None]:
    merged_entries: list[tuple[datetime, CheckItem]] = []
    in_flight_items: list[CheckItem] = []
    failed_items: list[CheckItem] = []
    seen_in_flight: set[int] = set()
    seen_failed: set[int] = set()
    earliest_since: datetime | None = None

    for overlay_name, window in overlay_windows.items():
        if earliest_since is None or window[0] < earliest_since:
            earliest_since = window[0]
        code_host, overlay_repos = overlay_configs.get(overlay_name, ("", []))
        scope = _MergedScope(overlay_name=overlay_name, code_host=code_host, overlay_repos=overlay_repos)
        overlay_tag = f"[{overlay_name}]"
        merged_entries.extend(
            merged_entries_from_qs(since=window[0], now=window[1], scope=scope, overlay_tag=overlay_tag)
        )
        new_in_flight, new_failed = motion_for_overlay(
            window=window,
            overlay_name=overlay_name,
            overlay_tag=overlay_tag,
            seen_in_flight=seen_in_flight,
            seen_failed=seen_failed,
        )
        in_flight_items.extend(new_in_flight)
        failed_items.extend(new_failed)

    merged_entries.sort(key=operator.itemgetter(0), reverse=True)
    return merged_entries, in_flight_items, failed_items, earliest_since


# ast-grep-ignore: ac-django-no-complexity-suppressions
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
        internal_escalations=internal_escalation_count(),
        metered=_metered_group(since=since, now=now, overlay_name=overlay_name, cap=cap),
        remaining=gather_remaining(
            now=now, overlay_names=[overlay_name] if overlay_name else None, tag_overlays=False, cap=cap
        ),
    )


def _metered_entries(
    *, since: datetime, now: datetime, overlay_name: str, cap: int, overlay_tag: str = ""
) -> tuple[list[tuple[datetime, CheckItem]], int]:
    attempts = TaskAttempt.objects.filter(
        ended_at__gte=since, ended_at__lt=now, lane=TaskAttempt.Lane.METERED
    ).order_by()
    if overlay_name:
        attempts = attempts.filter(task__ticket__overlay=overlay_name)
    grouped = attempts.values("task__ticket_id").annotate(
        latest=Max("ended_at"), count=Count("pk"), priced=Count("cost_usd"), total_cost=Sum("cost_usd")
    )
    total = grouped.count()
    rows = list(grouped.order_by("-latest")[:cap])
    tickets = Ticket.objects.in_bulk(row["task__ticket_id"] for row in rows)
    entries: list[tuple[datetime, CheckItem]] = []
    for row in rows:
        ticket = tickets[row["task__ticket_id"]]
        estimate = (
            f"estimated ${row['total_cost']:.2f}" if row["count"] == row["priced"] else "estimated cost unavailable"
        )
        count = f"{row['count']} attempts, " if row["count"] > 1 else ""
        entries.append(
            (
                row["latest"],
                CheckItem(
                    label=f"#{ticket.ticket_number}",
                    url=ticket_url(ticket),
                    detail=f"{count}{estimate} {overlay_tag}".strip(),
                    title=ticket.short_description,
                ),
            )
        )
    return entries, total


def _metered_group(*, since: datetime, now: datetime, overlay_name: str, cap: int) -> CheckGroup:
    entries, total = _metered_entries(since=since, now=now, overlay_name=overlay_name, cap=cap)
    return CheckGroup(title="Metered", items=[item for _, item in entries], total=total)


def _merged_group(*, since: datetime, now: datetime, scope: _MergedScope, cap: int) -> CheckGroup:
    items, total = merged_group_from_qs(since=since, now=now, scope=scope, cap=cap, overlay_tag="")
    return CheckGroup(title="Merged", items=items, total=total)


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
        tick = tr.ticket
        items.append(
            CheckItem(
                label=f"#{tick.ticket_number}",
                url=ticket_url(tick),
                detail=f"→ {tr.to_state}",
                title=tick.short_description,
            ),
        )
    items.sort(key=lambda item: int(item.label.lstrip("#")) if item.label.lstrip("#").isdigit() else 0, reverse=True)
    return CheckGroup(title="In-flight", items=items[:cap], total=len(items))


def _needs_you_group(*, since: datetime, now: datetime, overlay_name: str, cap: int) -> CheckGroup:
    overlay_slug = overlay_name or "<overlay>"
    items: list[CheckItem] = [*owner_wait_items(overlay_slug=overlay_slug, overlay=overlay_name)]

    failed = failed_attempts(since=since, now=now, overlay_name=overlay_name)
    seen_tickets: set[int] = set()
    for attempt in failed:
        tick = attempt.task.ticket
        if tick.pk in seen_tickets:
            continue
        seen_tickets.add(tick.pk)
        items.append(
            CheckItem(
                label=f"#{tick.ticket_number}",
                url=ticket_url(tick),
                detail="failed agent run",
                title=tick.short_description,
            ),
        )

    return CheckGroup(title="Needs you", items=items[:cap], total=len(items))
