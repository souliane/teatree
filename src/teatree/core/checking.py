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

Multi-overlay aggregation (``gather_all_overlays_report``):

:func:`gather_all_overlays_report` merges per-overlay groups into a single
:class:`AllOverlaysReport`. Each overlay-scoped item carries an ``[overlay]``
inline tag in its detail so the reader can tell provenance. The global
:class:`DeferredQuestion` query runs exactly once for the whole report — not
once per overlay — so a pending question never appears more than once.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypedDict

from teatree.core._checking_gather import (
    _MergedScope,
    deferred_questions,
    merged_group_from_qs,
    motion_for_overlay,
    ticket_url,
)
from teatree.core.models.task import TaskAttempt
from teatree.core.models.transition import TicketTransition

#: Per-group item cap; beyond it the renderer appends "…and X more".
DEFAULT_CAP = 5


class CheckItemDict(TypedDict):
    label: str
    url: str
    detail: str
    title: str


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


class AllOverlaysReportDict(TypedDict):
    all_overlays: list[str]
    earliest_since: str
    merged: CheckGroupDict
    in_flight: CheckGroupDict
    needs_you: CheckGroupDict
    terse: str


@dataclass(frozen=True, slots=True)
class CheckItem:
    """One line in a group — a clickable reference plus optional detail.

    ``url`` is the clickable target (a PR/issue/ticket URL, or an actionable
    command for a deferred question). ``title`` is the human-readable
    description rendered inline next to the id (#2092), so a reader never sees a
    bare/link-only ``#N`` they can't interpret. The shared
    :func:`teatree.core.ref_render.render_ref` chokepoint produces the
    ``[#N (short title)](url)`` shape every listing surface uses.
    """

    label: str
    url: str
    detail: str = ""
    title: str = ""

    def to_dict(self) -> CheckItemDict:
        return CheckItemDict(label=self.label, url=self.url, detail=self.detail, title=self.title)

    def render(self) -> str:
        from teatree.core.ref_render import render_ref  # noqa: PLC0415

        ref = render_ref(self.label, title=self.title, url=self.url)
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

    def to_dict(self) -> AllOverlaysReportDict:
        return AllOverlaysReportDict(
            all_overlays=self.all_overlays,
            earliest_since=self.earliest_since.isoformat(),
            merged=self.merged.to_dict(),
            in_flight=self.in_flight.to_dict(),
            needs_you=self.needs_you.to_dict(),
            terse=self.to_terse(),
        )

    def to_terse(self, *, cap: int = DEFAULT_CAP) -> str:
        """Render the terse view for all overlays.

        Header: ``Since <local HH:MM> · all overlays``.
        Empty report: ``Nothing since <local time>.``
        """
        from django.utils import timezone  # noqa: PLC0415

        local = (
            timezone.localtime(self.earliest_since) if timezone.is_aware(self.earliest_since) else self.earliest_since
        )
        stamp = local.strftime("%H:%M")
        groups = [self.merged, self.in_flight, self.needs_you]
        if all(group.total == 0 for group in groups):
            return f"Nothing since {stamp}."
        lines = [f"Since {stamp} · all overlays"]
        for group in groups:
            lines.extend(group.render(cap=cap))
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
    from teatree.core.merge import _looks_like_owner_repo  # noqa: PLC0415

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

    The global :class:`DeferredQuestion` query runs exactly ONCE — pending
    questions are not scoped per overlay, so repeating the query per overlay
    would duplicate them in the output.

    Overlay-scoped items (merged, in-flight, failed attempts) have their
    detail text suffixed with ``[overlay]`` so the reader sees provenance.
    """
    merged_items, in_flight_items, failed_items, earliest_since = _accumulate_overlays(
        overlay_windows=overlay_windows, overlay_configs=overlay_configs, cap=cap
    )
    in_flight_items.sort(
        key=lambda item: int(item.label.lstrip("#")) if item.label.lstrip("#").isdigit() else 0,
        reverse=True,
    )
    overlay_slug = next(iter(overlay_windows), "") or "<overlay>"
    needs_you_items = deferred_questions(overlay_slug=overlay_slug)
    needs_you_items.extend(failed_items)
    effective_since = earliest_since or next(iter(overlay_windows.values()), (None, None))[0] or datetime.now(UTC)
    return AllOverlaysReport(
        all_overlays=list(overlay_windows),
        earliest_since=effective_since,
        merged=CheckGroup(title="Merged", items=merged_items[:cap], total=len(merged_items)),
        in_flight=CheckGroup(title="In-flight", items=in_flight_items[:cap], total=len(in_flight_items)),
        needs_you=CheckGroup(title="Needs you", items=needs_you_items[:cap], total=len(needs_you_items)),
    )


def _accumulate_overlays(
    *,
    overlay_windows: dict[str, tuple[datetime, datetime]],
    overlay_configs: dict[str, tuple[str, list[str]]],
    cap: int,
) -> tuple[list[CheckItem], list[CheckItem], list[CheckItem], datetime | None]:
    merged_items: list[CheckItem] = []
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
        items, _ = merged_group_from_qs(since=window[0], now=window[1], scope=scope, cap=cap, overlay_tag=overlay_tag)
        merged_items.extend(items)
        new_in_flight, new_failed = motion_for_overlay(
            window=window,
            overlay_name=overlay_name,
            overlay_tag=overlay_tag,
            seen_in_flight=seen_in_flight,
            seen_failed=seen_failed,
        )
        in_flight_items.extend(new_in_flight)
        failed_items.extend(new_failed)

    return merged_items, in_flight_items, failed_items, earliest_since


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
    )


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
    items: list[CheckItem] = []

    overlay_slug = overlay_name or "<overlay>"
    items.extend(deferred_questions(overlay_slug=overlay_slug))

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
