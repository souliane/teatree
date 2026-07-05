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


def resolved_repo_key(audit: MergeAudit) -> tuple[str, int] | None:
    """The single canonical ``(owner/repo, pr_id)`` join key for a merge.

    Used at EVERY audit→ledger join site — S1 (against ``RedMrFixAttempt``) and
    S3 (against ``ReviewVerdict``) alike — so the two lanes can never diverge on
    how a merge maps to a repo (the ``_clear_repo_key`` vs ``_verdict_repo_key``
    split this replaces). Resolution, first match wins:

    (1) ``MergeAudit.repo_slug`` — the merge-time truth the gate stamped for the
        #1335-reconciled repo it actually merged against (#19). Present on every
        merge recorded after #19; the authoritative key a cross-repo merge is
        joined under, never the offline ticket-repo slug.
    (2) ``resolve_pr_repo_slug(clear)`` — the offline fallback for a legacy row
        with a blank ``repo_slug``. ``MergeClear.slug`` is a *workstream* slug
        under the dominant self-merge convention, so resolving-up to owner/repo
        (never ``normalize_repo_slug(clear.slug)``, which drops the workstream
        shape to ``""`` and collapses the sample) keeps that shape in the join. A
        degenerate CLEAR that cannot be resolved (workstream slug, no ticket
        ``issue_url``, no clone origin) is reported unmatched rather than joined
        on the wrong slug — never silently dropped from evidence.
    """
    stamped = (audit.repo_slug or "").strip()
    if stamped:
        return (stamped, audit.clear.pr_id)
    try:
        slug = resolve_pr_repo_slug(audit.clear)
    except MergePreconditionError:
        return None
    return (slug, audit.clear.pr_id)


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

    Each merge is joined to its CI-red record under its canonical owner/repo key
    (:func:`resolved_repo_key`), so the workstream-slug CLEAR of the dominant
    self-merge convention is resolved to its real repo and counts toward the
    denominator instead of collapsing the whole sample to ``insufficient_data``. A
    merge whose owner/repo cannot be resolved is routed to ``unmatched_slug`` and
    dropped from the denominator, the way S3 handles an unjoinable merge.
    """
    audits = _merge_audits_in(window, overlay)
    matchable: list[tuple[str, int]] = []
    unmatched = 0
    for audit in audits:
        key = resolved_repo_key(audit)
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
    # #17 anti-vacuity, mirroring S1's dead-recorder guard: a window with real
    # correction activity (red cards fired) but ZERO fix-classified tickets is
    # the fingerprint of a silent Kind.FIX writer — a correction should also mint
    # a FIX ticket. A 0-fix reading there is indistinguishable from a genuinely
    # defect-free window, so refuse the fabricated clean value and fail loud. A
    # window with NO correction activity at all is a legitimate clean reading.
    status = SignalStatus.OK
    if fix_created == 0 and red_card_count > 0:
        status = SignalStatus.INSTRUMENTATION_GAP
    return Computation(SignalReading(numerator / denom, denom, window.days, status), evidence)


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
    joined to its verdict under its canonical owner/repo key
    (:func:`resolved_repo_key`, the same key ``ReviewVerdict`` is stored under, so
    a cross-repo merge joins its verdict instead of a false-RED rubber-stamp
    miss); a merge whose owner/repo cannot be resolved is routed to
    ``unmatched_slug`` and dropped from the denominator, the way S1 handles an
    unjoinable merge — never mis-counted as a rubber-stamp.
    """
    audits = _merge_audits_in(window, overlay)
    matchable: list[tuple[str, int]] = []
    unmatched = 0
    for audit in audits:
        key = resolved_repo_key(audit)
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


def superseding_context(overlay: str) -> tuple[dict[tuple[str, int], datetime], set[tuple[str, int]]]:
    """The two supersede signals S4's staleness trip consults, each one grouped read (#15).

    ``(latest_issued, merged_keys)`` keyed on the raw ``MergeClear.slug`` (a
    re-CLEAR of the same workstream PR shares its older sibling's ``(slug,
    pr_id)``): the newest ``issued_at`` across ALL CLEARs for a key, and every
    ``(slug, pr_id)`` that already has a ``MergeAudit`` (the PR merged). Together
    they identify an unconsumed CLEAR the merge loop has moved past — a
    strictly-newer sibling re-reviewed it forward, or a merge already covers it.

    Public because the waiting-lane covering-CLEAR match (:func:`~teatree.core.waiting._has_covering_clear`,
    #21) reads the SAME context and applies the SAME :func:`clear_is_superseded`
    predicate — a superseded orphan must not authorise a merge there while S4
    excludes it here, or the two lanes diverge on the SIG-1 supersede semantics.
    An empty ``overlay`` scopes globally, which is what the per-PR waiting match
    wants so a ticket-less CLEAR's siblings are seen regardless of overlay.
    """
    clears = MergeClear.objects.all()
    audits = MergeAudit.objects.all()
    if overlay:
        clears = clears.filter(ticket__overlay=overlay)
        audits = audits.filter(clear__ticket__overlay=overlay)
    latest_issued: dict[tuple[str, int], datetime] = {}
    for slug, pr_id, issued_at in clears.values_list("slug", "pr_id", "issued_at"):
        key = (slug, pr_id)
        if key not in latest_issued or issued_at > latest_issued[key]:
            latest_issued[key] = issued_at
    merged_keys = {(slug, pr_id) for slug, pr_id in audits.values_list("clear__slug", "clear__pr_id")}
    return latest_issued, merged_keys


def clear_is_superseded(
    clear: MergeClear,
    latest_issued: dict[tuple[str, int], datetime],
    merged_keys: set[tuple[str, int]],
) -> bool:
    """True iff *clear* has been moved past — the shared SIG-1 supersede predicate (#15/#21).

    A CLEAR is superseded when a ``MergeAudit`` already covers its ``(slug,
    pr_id)`` (the PR merged) or a strictly-newer sibling CLEAR exists for the
    same key (a head-move re-review issued forward). The single predicate S4's
    staleness trip and the waiting-lane covering match both apply against a
    :func:`superseding_context`, so an orphaned old CLEAR is treated identically
    on both lanes instead of one counting it live and the other excluding it.
    """
    key = (clear.slug, clear.pr_id)
    if key in merged_keys:
        return True
    return latest_issued.get(key, clear.issued_at) > clear.issued_at


def _max_actionable_clear_age_hours(overlay: str, now: datetime) -> float | None:
    """Age in hours of the oldest actionable, non-superseded, unconsumed CLEAR, or ``None``.

    A CLEAR the merge loop has moved past is NOT a stalled merge and is excluded
    from the staleness trip (#15): a strictly-newer sibling CLEAR exists for the
    same ``(slug, pr_id)``, or a ``MergeAudit`` already covers that PR (the
    orphaned-row backstop to the merge-time sibling supersede in
    ``record_merge_and_advance``, catching a legacy or cross-tick sibling the
    supersede never reached). Without this, one head-move re-review left the older
    CLEAR unconsumed forever and ratcheted S4 hard-red permanently after 48h. A
    genuinely-stale CLEAR — no newer sibling, no covering merge — still trips.
    """
    qs = MergeClear.objects.filter(consumed_at__isnull=True).select_related("ticket")
    if overlay:
        qs = qs.filter(ticket__overlay=overlay)
    actionable = [clear for clear in qs if clear.is_actionable()]
    if not actionable:
        return None
    latest_issued, merged_keys = superseding_context(overlay)
    ages = [
        (now - clear.issued_at).total_seconds() / 3600.0
        for clear in actionable
        if not clear_is_superseded(clear, latest_issued, merged_keys)
    ]
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
        # #16: an envelope-refusal failure is recorded with exit_code=0 AND a
        # non-empty error, so classifying on exit_code alone counted N pure
        # refusals as a clean success group. Classify on the error field: a
        # genuine success is exit_code==0 with NO error; a non-zero/unknown
        # exit_code OR any error string is a failure. A None exit_code (attempt
        # in flight) is neither — excluded from both, unchanged. A named
        # follow-up will replace this overloaded exit_code+error read with an
        # explicit TaskAttempt outcome discriminator (success/refusal/crash) so a
        # refusal is a first-class terminal state rather than an inference (#16).
        is_failed = attempt.exit_code not in {None, 0} or bool(attempt.error)
        is_success = attempt.exit_code == 0 and not attempt.error
        if is_failed:
            failed += 1
        if is_success:
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
