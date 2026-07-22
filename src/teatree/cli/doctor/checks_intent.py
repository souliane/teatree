"""``_check_intent_freshness`` — the `t3 doctor` "no owner-intent silently rots" gate.

Doctor already alarms on stale dreams (``_check_dream_staleness``) and orphaned
issue-markers (``_check_marker_jam``), but nothing watched the INTENT queues — the
owner's directives and deferred questions. The incident this closes: the directive
loop sat masked for ~8 days while owner directives piled up at ``CAPTURED``, never
interpreted, producing ZERO signal — a masked/idle loop is indistinguishable from
"nothing to do" unless a check knows the queue is non-empty.

Two findings, split by severity to match the sibling checks:

HARD FAIL (gates the exit code) — a consumable intent queue is non-empty while its
consuming loop is not admitting (masked / disabled / held). This is the
silent-consumer bug: work exists, no live consumer will touch it. Mirrors the
self-heal silent-freeze detectors (``run_self_heal_checks``).

WARN (surfacing-only) — an item has sat in the queue past the freshness threshold
with a live consumer: owner intent is aging (the consumer runs but is not draining,
or the owner never came back to answer). Mirrors the dream / marker-jam advisories.

The pure :func:`intent_freshness_findings` takes injected state (a mockable clock,
pre-gathered queues) so the red→green contract is exercisable without a live loop
table; :func:`_check_intent_freshness` is the thin ORM-reading wrapper doctor runs.
The concrete queues are the directive intake arc and the owner deferred-question
delivery arc; :func:`_gather_intent_queues` is the extension seam for further
consumable queues.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import typer
from django.utils import timezone

#: One directive-loop cadence. The directive loop — and the daily intent-consuming
#: loops generally — run on an 86400s / 24h cron, so an intent item older than one
#: full cycle means the consumer never picked it up in a cadence it should have.
#: Shorter would false-alarm inside a single normal cycle; longer would let a whole
#: missed cadence pass unseen.
INTENT_FRESHNESS_THRESHOLD = timedelta(hours=24)

#: The pre-terminal directive states the directive loop still has to advance — the
#: intake arc where the incident's rot occurred (captured, never interpreted). A
#: directive here with a masked directive loop is intent with no live consumer.
_STUCK_DIRECTIVE_STATES: frozenset[str] = frozenset({"captured", "clarifying", "interpreted"})


@dataclass(frozen=True, slots=True)
class IntentItem:
    """One queued intent item: a human-readable ref and when it entered the queue."""

    ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IntentQueue:
    """One consumable intent queue and the liveness of the loop that drains it."""

    label: str
    consumer_loop: str
    remediation: str
    loop_admits: bool
    pending: tuple[IntentItem, ...]


@dataclass(frozen=True, slots=True)
class IntentFinding:
    """A rendered finding line plus whether it gates the doctor exit code."""

    message: str
    gating: bool


def _name_items(items: Iterable[IntentItem], *, now: datetime) -> str:
    return ", ".join(f"{item.ref} ({int((now - item.created_at) / timedelta(hours=1))}h)" for item in items)


def intent_freshness_findings(
    queues: Iterable[IntentQueue],
    *,
    now: datetime,
    threshold: timedelta = INTENT_FRESHNESS_THRESHOLD,
) -> list[IntentFinding]:
    """Findings for every intent queue whose work is stranded or aging.

    Per queue, first opinion wins: a non-empty queue whose consuming loop is not
    admitting is the FAIL-LOUD silent-consumer bug (gating) — the masked loop
    subsumes any staleness, so it short-circuits. Otherwise a live consumer that
    has let an item sit past *threshold* is a surfacing WARN (non-gating). An empty
    queue, or a fresh one with a live consumer, yields nothing.
    """
    findings: list[IntentFinding] = []
    for queue in queues:
        if not queue.pending:
            continue
        if not queue.loop_admits:
            findings.append(
                IntentFinding(
                    f"FAIL  {len(queue.pending)} {queue.label} item(s) pending but the "
                    f"{queue.consumer_loop!r} loop is not admitting — work exists with no live "
                    f"consumer, owner intent is rotting: {_name_items(queue.pending, now=now)}. "
                    f"Unmask it: {queue.remediation}.",
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
                    f"{queue.consumer_loop!r} loop is admitting — owner intent is aging: "
                    f"{_name_items(stale, now=now)}. Check why {queue.consumer_loop} is not draining them.",
                    gating=False,
                )
            )
    return findings


def _gather_intent_queues(loop_admits: dict[str, bool]) -> list[IntentQueue]:
    """The concrete consumable intent queues, read from the ORM (#no-owner-intent-rots).

    The directive intake arc (drained by the ``directive_loop`` tick) and the owner
    deferred-question delivery arc (mirrored to the owner by the ``dispatch`` loop's
    poster scanner). A loop absent from *loop_admits* is treated as not admitting,
    which only ever matters when its queue is non-empty — an unseeded loop table
    leaves both queues empty, so this never false-alarms.
    """
    from teatree.core.models import DeferredQuestion, Directive  # noqa: PLC0415 — ORM import needs the app registry

    directives = tuple(
        IntentItem(ref=f"directive #{pk}", created_at=created_at)
        for pk, created_at in Directive.objects.filter(state__in=_STUCK_DIRECTIVE_STATES)
        .order_by("created_at")
        .values_list("pk", "created_at")
    )
    questions = tuple(
        IntentItem(ref=f"question #{pk}", created_at=created_at)
        for pk, created_at in DeferredQuestion.pending()
        .filter(audience=DeferredQuestion.Audience.OWNER_QUESTION)
        .values_list("pk", "created_at")
    )
    return [
        IntentQueue(
            label="directive",
            consumer_loop="directive_loop",
            remediation="t3 loop enable directive_loop --emergency (see `t3 loop list` for the masking layer)",
            loop_admits=loop_admits.get("directive_loop", False),
            pending=directives,
        ),
        IntentQueue(
            label="owner-question",
            consumer_loop="dispatch",
            remediation="t3 loop enable dispatch --emergency (see `t3 loop list` for the masking layer)",
            loop_admits=loop_admits.get("dispatch", False),
            pending=questions,
        ),
    ]


def _check_intent_freshness() -> bool:
    """Fail loud when a non-empty intent queue has no live consumer (owner intent rot).

    HARD-FAILs (gates the exit code) when a consumable intent queue is non-empty
    while its consuming loop is masked/disabled/held — the exact silent-freeze the
    directive-loop incident produced. WARNs (surfacing-only) when a live consumer
    has let an item age past the freshness threshold. Crash-proof: any error (DB
    offline, unmigrated self-DB) degrades to OK so a doctor run never aborts on this
    check — same posture as the other DB-reading doctor checks.
    """
    from teatree.loops.preset_status import effective_verdicts  # noqa: PLC0415 — deferred: ORM read at call time

    try:
        now = timezone.now()
        loop_admits = {verdict.name: verdict.admitted for verdict in effective_verdicts(now)}
        findings = intent_freshness_findings(_gather_intent_queues(loop_admits), now=now)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Intent-freshness check crashed: {exc.__class__.__name__}: {exc}")
        return True  # degrades to OK: a crashed advisory read never reddens the run
    ok = True
    for finding in findings:
        typer.echo(finding.message)
        ok = ok and not finding.gating
    return ok
