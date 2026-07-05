"""Derived-on-read ledger queries for the factory signals (SIG-PR-1).

The query engine behind :mod:`teatree.core.factory_signals`: the low-level
value types (:class:`SignalReading`, :class:`Window`) plus the five per-signal
``_compute_s*`` functions that read the merge/review/CI/repair ledgers, and the
rolling-baseline regression predicates. Kept separate from the report-model +
composition concern so each file stays single-purpose; every function here is a
read-only ``select`` — no mutation, no LLM calls, no network.
"""

import enum
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any

from django.db.models import Min

from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.pr_slug_resolution import normalize_repo_slug, resolve_pr_repo_slug
from teatree.core.models.merge_clear import MergeAudit, MergeClear
from teatree.core.models.red_card_signal import RedCardSignal
from teatree.core.models.red_mr_fix_attempt import RedMrFixAttempt
from teatree.core.models.review_verdict import ReviewVerdict, Severity
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.utils.url_slug import pr_ref_from_url

# A five-observation floor keeps the thresholds stable at solo-factory volume;
# a stale actionable CLEAR older than STALE_CLEAR_HOURS is a stalled merge loop.
MIN_SAMPLE = 5
STALE_CLEAR_HOURS = 48.0

_CATCH_SEVERITIES = frozenset({Severity.BLOCKER, Severity.MAJOR})


class SignalStatus(enum.StrEnum):
    """The provider-seam status carried on every :class:`SignalReading`.

    ``instrumentation_gap`` is the fail-loud verdict for a provably-silent
    upstream recorder: it is NEVER collapsed to a clean ``ok`` at 100%, because
    a fabricated green is worse than admitting the measurement is blind.
    """

    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    INSTRUMENTATION_GAP = "instrumentation_gap"


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open ``[start, end)`` measurement window of *days* days."""

    start: datetime
    end: datetime
    days: int


@dataclass(frozen=True, slots=True)
class SignalReading:
    """One provider's reading — the seam PR-2's recipe registry consumes.

    ``value`` is the natural-unit measurement (a 0..1 rate for S1-S3, hours for
    S4, a mean iteration count for S5); the recipe normalises to 0..1 with the
    signal's direction. ``window_days`` is the trailing window width.
    """

    value: float
    sample_size: int
    window_days: int
    status: SignalStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "sample_size": self.sample_size,
            "window_days": self.window_days,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class Computation:
    """A reading plus its evidence and any companion hard-red trip.

    The public provider functions expose only :attr:`reading`;
    :func:`teatree.core.factory_signals.compute_factory_signals` also reads
    :attr:`evidence` (companion scalars) and :attr:`hard_red` (S4's stale-CLEAR
    trip, which fires even when the latency sample itself is insufficient).
    """

    reading: SignalReading
    evidence: dict[str, Any]
    hard_red: bool = False
    hard_red_reason: str = ""


def current_window(now: datetime, days: int) -> Window:
    return Window(now - timedelta(days=days), now, days)


def baseline_window(now: datetime, days: int) -> Window:
    return Window(now - timedelta(days=2 * days), now - timedelta(days=days), days)


def _merge_audits_in(window: Window, overlay: str) -> list[MergeAudit]:
    """Executed merges whose ``merged_at`` falls in *window*, with their CLEAR.

    Overlay scoping rides ``clear.ticket.overlay`` (the audit row carries no
    overlay of its own); a CLEAR with no ticket is out of an overlay-scoped
    view by construction.
    """
    qs = MergeAudit.objects.filter(merged_at__gte=window.start, merged_at__lt=window.end).select_related(
        "clear",
        "clear__ticket",
    )
    if overlay:
        qs = qs.filter(clear__ticket__overlay=overlay)
    return list(qs)


def _resolved_clear_key(clear: MergeClear) -> tuple[str, int] | None:
    """``(owner/repo, pr_id)`` for a CLEAR, resolved to the real repo it targets.

    ``MergeClear.slug`` is a *workstream* slug under teatree's dominant self-merge
    convention, not ``owner/repo`` — so both S1 (join against ``RedMrFixAttempt``)
    and S3 (join against ``ReviewVerdict``) resolve the CLEAR's real owner/repo via
    :func:`resolve_pr_repo_slug` (the same key the merge gate stores the PR under)
    before joining, offline (no network probe). Resolving-up beats stripping-down:
    the workstream slug carries no repo, so ``normalize_repo_slug(clear.slug)``
    would drop the dominant self-merge shape to ``""`` and collapse the sample. A
    degenerate CLEAR that cannot be resolved (workstream slug, no ticket
    ``issue_url``, no clone origin) is reported unmatched rather than joined on the
    wrong slug.
    """
    try:
        slug = resolve_pr_repo_slug(clear)
    except MergePreconditionError:
        return None
    return (slug, clear.pr_id)


def _red_pr_keys() -> tuple[set[tuple[str, int]], dict[tuple[str, int], set[str]]]:
    """Every ``(owner/repo, pr_number)`` with a recorded pre-merge CI-red attempt.

    ``RedMrFixAttempt`` is the only durable record of pre-merge redness on the
    loop's own PRs, so a merged PR absent from this set is first-try-green.
    Considers rows at any head (any ``dispatched_at``) — a PR's redness record
    is not window-bound. Also returns the distinct red head SHAs per PR for the
    ``re_ci_count`` companion.
    """
    keys: set[tuple[str, int]] = set()
    heads: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in RedMrFixAttempt.objects.all().only("pr_url", "head_sha"):
        ref = pr_ref_from_url(row.pr_url)
        if ref is None:
            continue
        slug = normalize_repo_slug(ref.slug)
        if not slug:
            continue
        key = (slug, ref.number)
        keys.add(key)
        heads[key].add(row.head_sha)
    return keys, heads


def compute_s1(window: Window, overlay: str, now: datetime) -> Computation:  # noqa: ARG001 — uniform compute signature
    """S1 first_try_green_rate: merged PRs with zero recorded CI-red fix attempts.

    Each merge is joined to its CI-red record under the CLEAR's resolved owner/repo
    slug (:func:`_resolved_clear_key`), so the workstream-slug CLEAR of the dominant
    self-merge convention is resolved to its real repo and counts toward the
    denominator instead of collapsing the whole sample to ``insufficient_data``. A
    CLEAR whose owner/repo cannot be resolved is routed to ``unmatched_slug`` and
    dropped from the denominator, the way S3 handles an unjoinable CLEAR.
    """
    audits = _merge_audits_in(window, overlay)
    matchable: list[tuple[str, int]] = []
    unmatched = 0
    for audit in audits:
        key = _resolved_clear_key(audit.clear)
        if key is None:
            unmatched += 1
        else:
            matchable.append(key)
    denom = len(matchable)
    evidence: dict[str, Any] = {"merges": denom, "unmatched_slug": unmatched}
    if denom < MIN_SAMPLE:
        return Computation(SignalReading(0.0, denom, window.days, SignalStatus.INSUFFICIENT_DATA), evidence)

    red_keys, red_heads = _red_pr_keys()
    green = sum(1 for key in matchable if key not in red_keys)
    rate = green / denom
    fix_in_window = RedMrFixAttempt.objects.filter(
        dispatched_at__gte=window.start,
        dispatched_at__lt=window.end,
    ).count()
    reci_values = [len(red_heads[key]) for key in matchable if key in red_heads]
    evidence |= {
        "first_try_green": green,
        "re_ci_count": round(mean(reci_values), 3) if reci_values else 0.0,
        "fix_attempts_in_window": fix_in_window,
    }
    # A perfect 100% that coincides with a completely silent recorder this
    # window is indistinguishable from a dead ``my_prs`` scanner — refuse the
    # fabricated green and fail loud.
    status = SignalStatus.OK
    if green == denom and fix_in_window == 0:
        status = SignalStatus.INSTRUMENTATION_GAP
    return Computation(SignalReading(rate, denom, window.days, status), evidence)


def _fix_tickets_created_in(window: Window, overlay: str) -> int:
    """Count fix-kind tickets first seen in *window*.

    ``Ticket`` carries no creation timestamp, so the earliest
    ``TicketTransition`` is the creation proxy — a fix ticket with no
    transitions yet is invisible to the window (documented scope, S2 risk 4).
    """
    qs = TicketTransition.objects.filter(ticket__kind=Ticket.Kind.FIX)
    if overlay:
        qs = qs.filter(ticket__overlay=overlay)
    grouped = (
        qs.values("ticket_id").annotate(first=Min("created_at")).filter(first__gte=window.start, first__lt=window.end)
    )
    return grouped.count()


def compute_s2(window: Window, overlay: str, now: datetime) -> Computation:  # noqa: ARG001 — uniform compute signature
    """S2 defect_escape_rate: fix tickets + red cards in *window* over PRECEDING merges.

    Window-level (not per-PR attribution): corrections lag the merge that
    caused them, so the denominator is the *preceding* window's merges.
    """
    denom_window = Window(window.start - timedelta(days=window.days), window.start, window.days)
    denom = len(_merge_audits_in(denom_window, overlay))
    fix_created = _fix_tickets_created_in(window, overlay)
    red_cards = RedCardSignal.objects.filter(observed_at__gte=window.start, observed_at__lt=window.end)
    if overlay:
        red_cards = red_cards.filter(overlay=overlay)
    red_card_count = red_cards.count()
    numerator = fix_created + red_card_count
    evidence: dict[str, Any] = {
        "fix_tickets": fix_created,
        "red_cards": red_card_count,
        "preceding_merges": denom,
    }
    if denom < MIN_SAMPLE:
        return Computation(SignalReading(0.0, denom, window.days, SignalStatus.INSUFFICIENT_DATA), evidence)
    return Computation(SignalReading(numerator / denom, denom, window.days, SignalStatus.OK), evidence)


def _review_caught(slug: str, pr_id: int) -> bool:
    """True iff any recorded verdict for the PR held or surfaced a blocker/major."""
    for verdict in ReviewVerdict.objects.for_pr(slug, pr_id):
        if verdict.verdict == ReviewVerdict.Verdict.HOLD:
            return True
        if any(finding.severity in _CATCH_SEVERITIES for finding in verdict.structured_findings):
            return True
    return False


def compute_s3(window: Window, overlay: str, now: datetime) -> Computation:  # noqa: ARG001 — uniform compute signature
    """S3 review_catch_rate: merged PRs whose review held or found a blocker/major.

    The rubber-stamp detector: ≥MIN_SAMPLE merges with a catch rate of zero is a
    vacuous review lane, tripped RED by the red-floor of ``0.0``. Each merge is
    joined to its verdict under the CLEAR's resolved owner/repo slug
    (:func:`_resolved_clear_key`, the key ``ReviewVerdict`` is stored under); a CLEAR
    whose owner/repo cannot be resolved is routed to ``unmatched_slug`` and dropped
    from the denominator, the way S1 handles an unjoinable CLEAR — never mis-counted
    as a rubber-stamp.
    """
    audits = _merge_audits_in(window, overlay)
    matchable: list[tuple[str, int]] = []
    unmatched = 0
    for audit in audits:
        key = _resolved_clear_key(audit.clear)
        if key is None:
            unmatched += 1
        else:
            matchable.append(key)
    denom = len(matchable)
    evidence: dict[str, Any] = {"merges": denom, "unmatched_slug": unmatched}
    if denom < MIN_SAMPLE:
        return Computation(SignalReading(0.0, denom, window.days, SignalStatus.INSUFFICIENT_DATA), evidence)
    caught = sum(1 for slug, pr_id in matchable if _review_caught(slug, pr_id))
    evidence["caught"] = caught
    return Computation(SignalReading(caught / denom, denom, window.days, SignalStatus.OK), evidence)


def _max_actionable_clear_age_hours(overlay: str, now: datetime) -> float | None:
    """Age in hours of the oldest actionable, unconsumed CLEAR, or ``None``."""
    qs = MergeClear.objects.filter(consumed_at__isnull=True).select_related("ticket")
    if overlay:
        qs = qs.filter(ticket__overlay=overlay)
    ages = [(now - clear.issued_at).total_seconds() / 3600.0 for clear in qs if clear.is_actionable()]
    return max(ages) if ages else None


def compute_s4(window: Window, overlay: str, now: datetime) -> Computation:
    """S4 merge_latency: median CLEAR→merge hours + stale-actionable-CLEAR age.

    The exact FK join ``MergeClear.issued_at`` → ``MergeAudit.merged_at``. The
    staleness companion is independent of the merge sample: an actionable CLEAR
    older than :data:`STALE_CLEAR_HOURS` trips RED even in a zero-merge window.
    """
    audits = _merge_audits_in(window, overlay)
    latencies = [
        (audit.merged_at - audit.clear.issued_at).total_seconds() / 3600.0
        for audit in audits
        if audit.merged_at >= audit.clear.issued_at
    ]
    denom = len(latencies)
    stale_hours = _max_actionable_clear_age_hours(overlay, now)
    hard_red = stale_hours is not None and stale_hours > STALE_CLEAR_HOURS
    evidence: dict[str, Any] = {
        "merges": denom,
        "stale_clear_hours": round(stale_hours, 2) if stale_hours is not None else 0.0,
    }
    reason = "actionable CLEAR older than 48h" if hard_red else ""
    if denom < MIN_SAMPLE:
        return Computation(
            SignalReading(0.0, denom, window.days, SignalStatus.INSUFFICIENT_DATA),
            evidence,
            hard_red=hard_red,
            hard_red_reason=reason,
        )
    return Computation(
        SignalReading(round(median(latencies), 3), denom, window.days, SignalStatus.OK),
        evidence,
        hard_red=hard_red,
        hard_red_reason=reason,
    )


def compute_s5(window: Window, overlay: str, now: datetime) -> Computation:  # noqa: ARG001 — uniform compute signature
    """S5 repair_iteration_burn: mean terminal iteration per succeeded (ticket, phase)."""
    attempts = TaskAttempt.objects.filter(
        started_at__gte=window.start,
        started_at__lt=window.end,
    ).select_related("task")
    if overlay:
        attempts = attempts.filter(task__ticket__overlay=overlay)
    rows = list(attempts)
    total = len(rows)
    groups: dict[tuple[int, str], list[int]] = defaultdict(list)
    failed = 0
    for attempt in rows:
        if attempt.exit_code not in {None, 0}:
            failed += 1
        if attempt.exit_code == 0:
            groups[attempt.task.ticket_id, attempt.task.phase].append(attempt.iteration)
    terminal_iters = [max(iters) for iters in groups.values()]
    sample = len(terminal_iters)
    evidence: dict[str, Any] = {
        "attempts": total,
        "success_groups": sample,
        "failed_fraction": round(failed / total, 3) if total else 0.0,
    }
    if sample < MIN_SAMPLE:
        return Computation(SignalReading(0.0, sample, window.days, SignalStatus.INSUFFICIENT_DATA), evidence)
    return Computation(SignalReading(round(mean(terminal_iters), 3), sample, window.days, SignalStatus.OK), evidence)
