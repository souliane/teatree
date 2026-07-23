"""``_check_intent_freshness`` — the `t3 doctor` "no owner-intent silently rots" gate.

Doctor already alarms on stale dreams (``_check_dream_staleness``) and orphaned
issue-markers (``_check_marker_jam``), but nothing watched the INTENT queues — the
owner's directives and deferred questions. The incident this closes: the directive
loop sat masked for ~8 days while owner directives piled up at ``CAPTURED``, never
interpreted, producing ZERO signal — a masked/idle loop is indistinguishable from
"nothing to do" unless a check knows the queue is non-empty.

Two findings, split by severity to match the sibling checks:

HARD FAIL (gates the exit code) — a consumable intent queue is non-empty while its
consumer is not live. This is the silent-consumer bug: work exists, nothing will
touch it. Mirrors the self-heal silent-freeze detectors (``run_self_heal_checks``).

WARN (surfacing-only) — an item has sat in the queue past the freshness threshold
with a live consumer: owner intent is aging (the consumer runs but is not draining,
or the owner never came back to answer). Mirrors the dream / marker-jam advisories.

Each queue is defined as exactly the rows its named consumer still has to drain — a row
parked on an unanswered human gate (a delivered question, a directive awaiting its clarify
answers) is the owner's work, not the consumer's — and "live" means every gate that consumer
actually passes: an unmasked loop row whose guard chain refuses is NOT a live consumer.

The pure :func:`intent_freshness_findings` takes injected state (a mockable clock,
pre-gathered queues) so the red→green contract is exercisable without a live loop
table; :func:`_check_intent_freshness` is the thin ORM-reading wrapper doctor runs.
:func:`_gather_intent_queues` is the extension seam for further consumable queues.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import typer
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.loops.directive_loop.guards import DirectiveLoopSettings
    from teatree.loops.outer_loop.guards import GuardSeams

#: One directive-loop cadence. The directive loop — and the daily intent-consuming
#: loops generally — run on an 86400s / 24h cron, so an intent item older than one
#: full cycle means the consumer never picked it up in a cadence it should have.
#: Shorter would false-alarm inside a single normal cycle; longer would let a whole
#: missed cadence pass unseen.
INTENT_FRESHNESS_THRESHOLD = timedelta(hours=24)

#: How many queued refs a finding names before it collapses into "and N more". The
#: message is content-hashed into the watchdog's idempotency key, so it stays bounded
#: however deep the queue gets.
INTENT_ITEM_CAP = 5


@dataclass(frozen=True, slots=True)
class IntentItem:
    """One queued intent item: a human-readable ref and when it entered the queue."""

    ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IntentQueue:
    """One consumable intent queue and the liveness of the consumer that drains it."""

    label: str
    consumer_loop: str
    remediation: str
    consumer_live: bool
    pending: tuple[IntentItem, ...]


@dataclass(frozen=True, slots=True)
class IntentFinding:
    """A rendered finding line plus whether it gates the doctor exit code."""

    message: str
    gating: bool


def _name_items(items: Sequence[IntentItem]) -> str:
    """Name at most :data:`INTENT_ITEM_CAP` refs, with an "and N more" tail.

    Deliberately age-free: the watchdog content-hashes the finding into its
    idempotency key, so an age that ticks hourly would re-DM the owner every hour
    the queue sits. The refs are stable identity; the threshold in the WARN text
    already carries the "how long" the reader needs.
    """
    named = ", ".join(item.ref for item in items[:INTENT_ITEM_CAP])
    remaining = len(items) - INTENT_ITEM_CAP
    return f"{named} and {remaining} more" if remaining > 0 else named


def intent_freshness_findings(
    queues: Iterable[IntentQueue],
    *,
    now: datetime,
    threshold: timedelta = INTENT_FRESHNESS_THRESHOLD,
) -> list[IntentFinding]:
    """Findings for every intent queue whose work is stranded or aging.

    Per queue, first opinion wins: a non-empty queue with no live consumer is the
    FAIL-LOUD silent-consumer bug (gating) — a dead consumer subsumes any staleness,
    so it short-circuits. Otherwise a live consumer that has let an item sit past
    *threshold* is a surfacing WARN (non-gating). An empty queue, or a fresh one with
    a live consumer, yields nothing.
    """
    findings: list[IntentFinding] = []
    for queue in queues:
        if not queue.pending:
            continue
        if not queue.consumer_live:
            findings.append(
                IntentFinding(
                    f"FAIL  {len(queue.pending)} {queue.label} item(s) pending but the "
                    f"{queue.consumer_loop!r} consumer is not live — work exists with no live "
                    f"consumer, owner intent is rotting: {_name_items(queue.pending)}. "
                    f"{queue.remediation}",
                    gating=True,
                )
            )
            continue
        stale = tuple(item for item in queue.pending if now - item.created_at >= threshold)
        if stale:
            hours = int(threshold / timedelta(hours=1))
            findings.append(
                IntentFinding(
                    f"WARN  {len(stale)} {queue.label} item(s) have sat past {hours}h while the "
                    f"{queue.consumer_loop!r} consumer is live — owner intent is aging: "
                    f"{_name_items(stale)}. Check why {queue.consumer_loop} is not draining them.",
                    gating=False,
                )
            )
    return findings


def _directive_consumer_liveness(
    *,
    loop_admits: bool,
    settings: "DirectiveLoopSettings | None",
    seams: "GuardSeams | None",
) -> tuple[bool, str]:
    """Whether the directive queue has a live consumer, and what must change if not.

    An unmasked loop row is only half the gate: every directive tick first runs the
    fail-closed guard chain (the DARK ``directive_loop_enabled`` flag, the critic-live
    probe, signal trust, the self-improve budget), and all of them ship off — so a queue
    whose loop row is enabled can still have no consumer at all. The remediation names
    both blockers, so following it cannot silence the finding while directives still
    never advance.

    The chain probed is the INTAKE one: this queue holds the pre-admission arc — the rows
    the tick interprets before stopping at the structural human ratify gate. The
    post-admission ``evaluate_execution_guards`` additionally gates on
    ``factory_score_enabled``, which never blocks intake, so probing that chain would
    report a consumer as dead while it is in fact draining this queue.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: DB read at call time
    from teatree.loops.directive_loop.guards import (  # noqa: PLC0415 — deferred: ORM-backed probes
        CRITIC_NOT_LIVE,
        FLAG_OFF,
        SIGNAL_UNTRUSTED,
        evaluate_intake_guards,
    )

    blockers: list[str] = []
    if not loop_admits:
        blockers.append("unmask the loop row: t3 loop enable directive_loop --emergency")
    verdict = evaluate_intake_guards(
        settings=settings if settings is not None else get_effective_settings(None), seams=seams
    )
    if not verdict.ok:
        remedies = {
            FLAG_OFF: "turn on the DARK `directive_loop_enabled` setting",
            CRITIC_NOT_LIVE: "make the critic gate a proven live merge supervisor",
            SIGNAL_UNTRUSTED: "close the factory-signal instrumentation gap",
        }
        remedy = remedies.get(verdict.reason, f"clear the {verdict.reason.split(':', 1)[0]} refusal")
        blockers.append(f"clear the guard refusal {verdict.reason!r} — {remedy}")
    return not blockers, "To restore the consumer: " + "; ".join(blockers) + "."


def _drainable_directives() -> tuple[IntentItem, ...]:
    """The directive rows the ``directive_loop`` tick still has to advance itself.

    ``CLARIFYING`` is the human-gated exception: ``_advance_clarifying`` re-interprets
    only once EVERY clarify question of the current generation is answered, and otherwise
    waits on the owner — so an unanswered row is the owner's work, exactly like a
    DELIVERED-but-unanswered question. ``RATIFY_PENDING`` is excluded for the same reason
    by never entering the stuck set at all.
    """
    from teatree.core.models import Directive  # noqa: PLC0415 — ORM import needs the app registry
    from teatree.loops.directive_loop.interpret import (  # noqa: PLC0415 — deferred: ORM-backed probe
        clarifications_answered,
    )

    stuck_directive_states = frozenset(
        {Directive.State.CAPTURED, Directive.State.CLARIFYING, Directive.State.INTERPRETED}
    )
    rows = (
        Directive.objects.filter(state__in=stuck_directive_states)
        .order_by("created_at")
        .only("pk", "created_at", "state", "generation")
    )
    return tuple(
        IntentItem(ref=f"directive #{row.pk}", created_at=row.created_at)
        for row in rows
        if row.state != Directive.State.CLARIFYING or clarifications_answered(row)
    )


def _gather_intent_queues(
    loop_admits: dict[str, bool],
    *,
    settings: "DirectiveLoopSettings | None" = None,
    seams: "GuardSeams | None" = None,
) -> list[IntentQueue]:
    """The concrete consumable intent queues, read from the ORM (#no-owner-intent-rots).

    The directive intake arc (the rows the ``directive_loop`` tick still advances itself
    — see :func:`_drainable_directives`) and the owner deferred-question DELIVERY arc
    (mirrored to the owner by the ``dispatch`` loop's poster scanner, which drains exactly
    the un-mirrored rows — a delivered question stays pending until the HUMAN answers, so
    it is no longer this consumer's work). A loop absent from *loop_admits* is treated as
    not admitting, which only ever matters when its queue is non-empty — an unseeded loop
    table leaves both queues empty, so this never false-alarms.
    """
    from teatree.core.models import DeferredQuestion  # noqa: PLC0415 — ORM import needs the app registry

    directives = _drainable_directives()
    questions = tuple(
        IntentItem(ref=f"question #{pk}", created_at=created_at)
        for pk, created_at in DeferredQuestion.unmirrored_pending().values_list("pk", "created_at")
    )
    # The guard chain probes the critic, the factory signals and the budget, so only
    # pay for it when there is a directive whose liveness verdict could matter.
    directive_live, directive_remediation = (
        _directive_consumer_liveness(
            loop_admits=loop_admits.get("directive_loop", False), settings=settings, seams=seams
        )
        if directives
        else (True, "")
    )
    return [
        IntentQueue(
            label="directive",
            consumer_loop="directive_loop",
            remediation=directive_remediation,
            consumer_live=directive_live,
            pending=directives,
        ),
        IntentQueue(
            label="owner-question",
            consumer_loop="dispatch",
            remediation=(
                "To restore the consumer: unmask the loop row: "
                "t3 loop enable dispatch --emergency (see `t3 loop list` for the masking layer)."
            ),
            consumer_live=loop_admits.get("dispatch", False),
            pending=questions,
        ),
    ]


def _check_intent_freshness(
    *,
    settings: "DirectiveLoopSettings | None" = None,
    seams: "GuardSeams | None" = None,
) -> bool:
    """Fail loud when a non-empty intent queue has no live consumer (owner intent rot).

    HARD-FAILs (gates the exit code) when a consumable intent queue is non-empty while
    its consumer is not live — the exact silent-freeze the directive-loop incident
    produced. WARNs (surfacing-only) when a live consumer has let an item age past the
    freshness threshold. *settings* / *seams* are the directive guard chain's injection
    points, exactly as on ``directive_loop.run_tick``; doctor passes neither. Crash-proof:
    any error (DB offline, unmigrated self-DB) degrades to OK so a doctor run never
    aborts on this check — same posture as the other DB-reading doctor checks.
    """
    try:
        from teatree.loops.preset_status import effective_verdicts  # noqa: PLC0415 — deferred: ORM read at call time

        now = timezone.now()
        loop_admits = {verdict.name: verdict.admitted for verdict in effective_verdicts(now)}
        queues = _gather_intent_queues(loop_admits, settings=settings, seams=seams)
        findings = intent_freshness_findings(queues, now=now)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Intent-freshness check crashed: {exc.__class__.__name__}: {exc}")
        return True  # degrades to OK: a crashed advisory read never reddens the run
    ok = True
    for finding in findings:
        typer.echo(finding.message)
        ok = ok and not finding.gating
    return ok
