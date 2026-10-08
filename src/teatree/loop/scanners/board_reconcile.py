"""The board janitor: reconcile the ticket FSM toward forge truth (#3841, #3840).

The FSM is teatree's model of the world; the forge is the world. Nothing drove the
model back toward the world, so the board degraded monotonically — measured live at
205 tickets in ``review_delivered``, 55 in ``not_started`` and exactly ONE in ``merged``
across 328 rows, with six PRs merged in one minute and not a single card moving.

The FSM was NOT the blocker. ``Ticket.reconcile_merged`` already accepts every
pre-merged state, so ``not_started → merged`` is one legal hop. Its two callers were
both structurally starved: the merge keystone advances ``clear.ticket``, and 469 of
470 ``MergeClear`` rows on this box carry ``ticket_id = NULL`` (the single row that
does not is the single ``merged`` ticket); the per-tick sweep keys on a linked
``PullRequest`` row in MERGED, and the box has ten PR rows, all OPEN. So the missing
piece is a driver that asks the FORGE about the ticket's own item — which is what
rule B below is. (The 205 ``review_delivered`` rows are all ``role = reviewer``: that
state is the reviewer terminal, so they are correctly not merge candidates.)

Six rules, one path, applied in cheapest-first order:

Rule A — a linked ``PullRequest`` row is MERGED, unless the ticket was reopened over
    every such row (#5031). The #3540 sweep: a ticket entered via a non-ladder phase
    whose PR merged outside the keystone has no automatic exit from its entry state.
    The scan reads no forge, but the merge-evidence gate does when none is stored.
Rule B — the ticket's OWN ``issue_url`` names a PR the forge says MERGED. The
    load-bearing rule: the gap rule A cannot see is a ticket with no ``PullRequest``
    row at all, which is the shape of every card the board rendered as NOT STARTED
    behind a merged PR.
Rule C — that same PR is CLOSED unmerged: abandoned, so the ticket resolves to
    ``IGNORED`` rather than sitting on the board forever.
Rule D — the upstream ISSUE is done for a post-ship ticket. The NARROW walk
    ``t3 <overlay> ticket sync-completions`` already carried (pr_opened/review_requested/
    merged only, which is why it reported "No tickets to advance" against this
    board). Preserved verbatim as one rule so there is a single reconciliation path
    rather than a sibling — it is not the rule that drains the backlog.
Rule E — the upstream ISSUE behind a DELIVERED ticket was REOPENED. DELIVERED is
    terminal and a ticket owns its issue URL in every state but IGNORED, so intake
    could never re-admit that issue and no other rule could reach the ticket — the
    issue was stranded silently, forever (#4152). The revived ticket lands on WORK_STARTED
    and the hard-bounded ``stuck_ticket_redispatch`` sweep schedules its planning.
Rule F — a PRE-SHIP ticket whose own ISSUE the forge says CLOSED. The shape no
    other rule can reach, because B/C read an issue URL as an unknown PR, D polls
    only the post-ship states, and E only DELIVERED — so a backlog prune left twelve
    rows ``plan_recorded`` behind closed issues, each a permanent dispatch source (#4711).
    Rules E and F live in the sibling ``board_reconcile_issue_reopen`` and
    ``board_reconcile_issue_close`` modules; every rule calls the shared application
    machinery in ``board_reconcile_apply``.

Every rule is idempotent by construction: each candidate queryset excludes the state
its rule targets, so a second consecutive run finds nothing. Forge reads are
fail-CLOSED — an unreachable forge is UNKNOWN, never "merged" — and bounded by
``probe_budget`` per run so the janitor can never saturate the box.

:class:`BoardReconcileScanner` is the cadenced host, registered in the
``housekeeping`` domain. The host must NOT be ``colleague_facing``: such a loop is
skipped under an away-class mode, and a board janitor has to run hardest when nobody
is watching. The scanner emits one signal per APPLIED transition — never per
candidate examined — so the evidence a card moved is the transition itself and not
the tick's own success message (this box has a tick reporting ``58 signal(s), 58
action(s)`` while nothing advanced).

The reconcile and its scanner share this module so the per-tick render pass, the
``t3 <overlay> ticket sync-completions`` command and the cadenced loop all call one
function; it sits under ``teatree.loop.scanners`` because that is the layer permitted
to compose ``teatree.core`` FSM transitions with ``teatree.backends`` forge reads.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_fsm import can_proceed

from teatree.backends.issue_reads import issue_is_done
from teatree.backends.loader import pr_open_state
from teatree.core.backend_protocols import PrOpenState
from teatree.core.factory.health_signal import HealthSignal
from teatree.core.models.known_issue import KnownIssue, merge_refusal_fingerprint
from teatree.core.models.ticket_ledger import all_merges_reopened_over
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.board_reconcile_apply import collect, planned, scoped
from teatree.loop.scanners.board_reconcile_issue_close import closed_issue_transitions
from teatree.loop.scanners.board_reconcile_issue_reopen import reopened_issue_transitions
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardReconcileReport, BoardTransition
from teatree.url_classify import Forge, forge_of

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)

#: Forge reads one run may issue. The whole candidate set on the incident board was
#: ~57 tickets, so this covers it outright while still bounding an unbounded backlog.
DEFAULT_PROBE_BUDGET = 150


@dataclass(slots=True)
class BoardReconcileScanner:
    """Reconcile the ticket FSM against forge truth, reporting each transition."""

    overlay_name: str = ""
    name: str = "board_reconcile"

    def scan(self) -> list[ScanSignal]:
        report = reconcile_board(overlay=self.overlay_name)
        for line in report.lines():
            logger.info("board_reconcile: %s", line)
        return [
            ScanSignal(
                kind="board.reconciled",
                summary=f"Ticket #{t.ticket_id} {t.from_state} → {t.to_state} ({t.reason})",
                payload={
                    "ticket_id": t.ticket_id,
                    "issue_url": t.issue_url,
                    "from_state": t.from_state,
                    "to_state": t.to_state,
                    "action": str(t.action),
                    "reason": t.reason,
                },
            )
            for t in report.applied
        ]


def reconcile_board(
    *,
    overlay: str = "",
    dry_run: bool = False,
    probe_forge: bool = True,
    probe_budget: int = DEFAULT_PROBE_BUDGET,
) -> BoardReconcileReport:
    """Reconcile every stale ticket against forge truth, returning what changed.

    *overlay* scopes the sweep to one overlay's tickets, so a multi-overlay fan-out
    PARTITIONS the work instead of each slice re-probing the whole board; blank
    sweeps every overlay. *probe_forge* off runs only the no-network rule A, which
    is what the per-tick render pass wants; the cadenced scanner and the CLI run the
    full set.
    """
    transitions = list(_merged_pr_row_transitions(overlay=overlay, dry_run=dry_run))
    probes = 0
    if probe_forge:
        forge_transitions, probes = _forge_truth_transitions(
            overlay=overlay, dry_run=dry_run, probe_budget=probe_budget
        )
        transitions.extend(forge_transitions)
    return BoardReconcileReport(transitions=tuple(transitions), probes=probes, dry_run=dry_run)


def _merged_pr_row_transitions(*, overlay: str, dry_run: bool) -> list[BoardTransition]:
    """Rule A — every ticket with a MERGED ``PullRequest`` row not yet at its own terminal.

    Both target states are excluded, one per role branch: ``MERGED`` for the author
    branch and ``REVIEW_DELIVERED`` for the reviewer branch. Excluding only the former
    left the reviewer lane non-idempotent — ``mark_review_no_action`` accepts
    ``REVIEW_DELIVERED`` as a source (the #1431 self-transition), so a reviewer ticket
    this rule had already correctly closed stayed a candidate forever, re-emitting an
    applied transition with ``from_state == to_state`` on every tick. The rest of each
    transition's source membership — which pointedly refuses the post-merged /
    abandoned states so the FSM is never dragged backward — stays enforced per row by
    the ``can_proceed`` guards, keeping the FSM the single source of truth.

    A reopened ticket keeps its MERGED rows, so without the ``all_merges_reopened_over``
    skip this rule re-merged it within a tick and re-enqueued its worktree teardown.
    """
    from teatree.core.models import PullRequest, Ticket  # noqa: PLC0415 — ORM import needs the app registry

    candidates = scoped(
        Ticket.objects.filter(pull_requests__state=PullRequest.State.MERGED).exclude(
            state__in=(Ticket.State.MERGED, Ticket.State.REVIEW_DELIVERED)
        ),
        overlay,
    ).distinct()
    return collect(
        candidates,
        lambda ticket: (
            None
            if all_merges_reopened_over(ticket)
            else _on_merge_signal(ticket, reason="merged PR row", dry_run=dry_run)
        ),
    )


def _forge_truth_transitions(*, overlay: str, dry_run: bool, probe_budget: int) -> tuple[list[BoardTransition], int]:
    """Rules B/C/D/E/F — the live forge reads, bounded by *probe_budget*.

    Newest-ticket-first, because a freshly merged PR is what makes the board
    untrustworthy minute to minute; the budget is what keeps an unbounded backlog
    from turning the janitor into the thing that saturates the box. Rule E's DELIVERED
    pool is typically far larger than rule F's pre-ship pool, so letting E spend the
    whole post-B/C remainder (the original split) starved F to near zero on a live
    board — measured at 2 probes/run against 46 candidates (#4711 follow-up). Rule E is
    now CAPPED at half of what B/C left, OR at rule F's own live candidate count,
    whichever is smaller — a fixed half still stranded probes whenever F's queue was
    shorter than its reservation (measured: 5 probes spent of 10 available when F had 0
    candidates against 10 rule-E ones). Only E's unused share rolls forward to F, because
    E runs first; F's own unused share can never roll back to E, since E has already
    spent its capped share by the time F runs. The reservation is a floor, not a
    guarantee at every budget: ``remaining // 2`` is 0 when *remaining* is 0 or 1, so at
    those smallest budgets rule E can still take the only probe even when F has work.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    candidates = scoped(
        Ticket.objects.exclude(issue_url="").exclude(state__in=_settled_states()).filter(remote_missing=False),
        overlay,
    ).order_by("-pk")
    pr_tickets = [t for t in candidates if forge_of(t.issue_url) is not Forge.UNKNOWN][:probe_budget]
    states = {ticket.issue_url: pr_open_state(ticket.issue_url) for ticket in pr_tickets}

    transitions = collect(pr_tickets, lambda ticket: _from_pr_state(ticket, states, dry_run=dry_run))
    transitions.extend(_issue_done_transitions(overlay=overlay, dry_run=dry_run))
    remaining = probe_budget - len(states)
    reserved_for_f = min(max(remaining // 2, 0), _rule_f_candidate_count(overlay))
    reopened, reopen_probes = reopened_issue_transitions(
        overlay=overlay, dry_run=dry_run, probe_budget=remaining - reserved_for_f
    )
    transitions.extend(reopened)
    closed, close_probes = closed_issue_transitions(
        overlay=overlay,
        dry_run=dry_run,
        probe_budget=remaining - reopen_probes,
        already_moved=frozenset(t.ticket_id for t in transitions if t.applied),
    )
    transitions.extend(closed)
    return transitions, len(states) + reopen_probes + close_probes


def _rule_f_candidate_count(overlay: str) -> int:
    """Rule F's raw pre-ship candidate count — an upper bound for its probe reservation.

    A plain DB ``count()``, never a forge read, so it costs nothing to call before E
    runs. It is an upper bound rather than an exact figure: the real
    ``already_moved`` exclusion (rows E revives this same tick) is not known yet, but
    that can only make the reservation too generous, never starve F below what it
    will actually spend.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    return scoped(
        Ticket.objects.filter(state__in=Ticket.pre_ship_states())
        .exclude(issue_url="")
        .exclude(role=Ticket.Role.REVIEWER)
        .filter(remote_missing=False),
        overlay,
    ).count()


def _settled_states() -> frozenset[str]:
    """States no forge probe can usefully move — the candidate filter for rules B/C only.

    MERGED-or-past plus the abandoned and reviewer terminals. Rule D does NOT read this
    set (it builds its own candidates from ``completable_states()``), so keeping MERGED
    out of it bought nothing and cost one forge probe per merged ticket per run: rule B
    cannot advance a MERGED ticket and rule C must not drag one to IGNORED.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    return Ticket.merged_states() | {Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED}


def _from_pr_state(
    ticket: "Ticket",
    states: dict[str, PrOpenState],
    *,
    dry_run: bool,
) -> BoardTransition | None:
    """Rules B and C — act only on a DEFINITE forge verdict.

    ``OPEN`` means the work is genuinely in flight and ``UNKNOWN`` means the read
    failed; both leave the ticket exactly where it is, because believing a ticket
    landed when it did not is the failure this whole path exists to prevent.
    """
    state = states.get(ticket.issue_url, PrOpenState.UNKNOWN)
    if state is PrOpenState.MERGED:
        return _on_merge_signal(ticket, reason="forge says the PR merged", dry_run=dry_run)
    if state is PrOpenState.CLOSED:
        return _on_close_signal(ticket, reason="forge says the PR closed unmerged", dry_run=dry_run)
    return None


def _is_reviewer(ticket: "Ticket") -> bool:
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    return ticket.role == Ticket.Role.REVIEWER


def _on_merge_signal(ticket: "Ticket", *, reason: str, dry_run: bool) -> BoardTransition | None:
    """Route a merged-PR signal by ROLE — the author merged it; the reviewer only read it.

    A reviewer ticket tracks teatree's review of SOMEONE ELSE's PR, so landing it on
    MERGED would claim authorship of work teatree never wrote and would enqueue a
    spurious worktree teardown. ``REVIEW_DELIVERED`` is the reviewer terminal (and the
    board hides it) — the same reason that state is absent from the reconcile sources.
    """
    if _is_reviewer(ticket):
        return _close_review(ticket, reason=reason, dry_run=dry_run)
    return _advance_to_merged(ticket, reason=reason, dry_run=dry_run)


def _on_close_signal(ticket: "Ticket", *, reason: str, dry_run: bool) -> BoardTransition | None:
    """Route a closed-unmerged signal by role: an author ticket is abandoned, a review is moot.

    A MERGED ticket is neither — it LANDED, and ``ignore()`` accepts MERGED as a source,
    so a CLOSED verdict could undo a merge. That is excluded at the candidate queryset
    (:func:`_settled_states`) rather than guarded here, so there is one mechanism and no
    unreachable branch.
    """
    if _is_reviewer(ticket):
        return _close_review(ticket, reason=reason, dry_run=dry_run)
    return _resolve_ignored(ticket, reason=reason, dry_run=dry_run)


def _close_review(ticket: "Ticket", *, reason: str, dry_run: bool) -> BoardTransition | None:
    """Land a reviewer ticket on its own terminal — the PR is decided, so no review can post."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    if not can_proceed(ticket.mark_review_no_action):
        return None
    from_state = ticket.state
    if dry_run:
        return planned(ticket, Ticket.State.REVIEW_DELIVERED, BoardAction.REVIEW_CLOSED, reason)
    ticket.mark_review_no_action()
    ticket.save()
    logger.info("Board reconcile closed review ticket %s %s → review_delivered (%s)", ticket.pk, from_state, reason)
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=ticket.state,
        action=BoardAction.REVIEW_CLOSED,
        reason=reason,
        applied=True,
    )


def _issue_done_transitions(*, overlay: str, dry_run: bool) -> list[BoardTransition]:
    """Rule D — post-ship AUTHOR tickets whose upstream issue the overlay calls done.

    Reviewer tickets are excluded rather than routed to ``_close_review``, because
    ``advance_to_delivered`` IS the author ladder: its walk transits MERGED, so a
    reviewer ticket admitted here becomes the same irreversible ghost rules A/B/C
    guard against. No reviewer terminal is reachable from the completable states
    either — ``mark_review_no_action`` does not accept PR_OPENED/REVIEW_REQUESTED/MERGED — so
    skipping is the whole correct action, not a deferral.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    candidates = list(
        scoped(
            Ticket.objects.filter(state__in=Ticket.completable_states())
            .exclude(issue_url="")
            .exclude(role=Ticket.Role.REVIEWER)
            .filter(remote_missing=False),
            overlay,
        )
    )
    done = _issue_done_urls(candidates)
    return collect(
        [t for t in candidates if t.issue_url in done],
        lambda ticket: _advance_to_delivered(ticket, dry_run=dry_run),
    )


def _issue_done_urls(tickets: "list[Ticket]") -> set[str]:
    """The subset of *tickets*' issue URLs the owning overlay reports as done.

    Grouped by the ticket's own overlay so each URL is judged by the overlay that
    owns it; a ticket whose overlay is not installed here is simply not judged.
    """
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: registry read at call time

    overlays = get_all_overlays()
    return {
        ticket.issue_url
        for ticket in tickets
        if ticket.overlay in overlays and issue_is_done(overlays[ticket.overlay], ticket.issue_url)
    }


def _advance_to_merged(ticket: "Ticket", *, reason: str, dry_run: bool) -> BoardTransition | None:
    """Drive one ticket to MERGED, or report the intent under *dry_run*."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    if ticket.state == Ticket.State.MERGED or not can_proceed(ticket.reconcile_merged):
        return None
    from_state = ticket.state
    if dry_run:
        return planned(ticket, Ticket.State.MERGED, BoardAction.ADVANCED_MERGED, reason)
    from teatree.core.gates.merge_evidence_gate import (  # noqa: PLC0415 — ORM import after app setup
        record_confirmed_forge_merge,
    )

    if not record_confirmed_forge_merge(ticket):
        error = "forge reported MERGED without a verifiable merged commit SHA"
        issue = KnownIssue.objects.record_signal(
            HealthSignal(
                fingerprint=merge_refusal_fingerprint(ticket.pk),
                severity=KnownIssue.Severity.WARNING,
                kind="board_merge_refused",
                overlay=ticket.overlay,
                summary=f"Ticket #{ticket.pk}: {error}",
                evidence_url=ticket.issue_url,
            )
        )
        if issue.auto_resolve:
            issue.auto_resolve = False
            issue.save(update_fields=["auto_resolve"])
        return BoardTransition(
            ticket_id=int(ticket.pk),
            issue_url=ticket.issue_url,
            from_state=from_state,
            to_state=from_state,
            action=BoardAction.REFUSED,
            reason=reason,
            applied=False,
            error=error,
        )
    ticket.reconcile_merged()
    ticket.save()
    logger.info("Board reconcile advanced ticket %s %s → merged (%s)", ticket.pk, from_state, reason)
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=ticket.state,
        action=BoardAction.ADVANCED_MERGED,
        reason=reason,
        applied=True,
    )


def _resolve_ignored(ticket: "Ticket", *, reason: str, dry_run: bool) -> BoardTransition | None:
    """Resolve one abandoned ticket to IGNORED, or report the intent under *dry_run*."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    if not can_proceed(ticket.ignore):
        return None
    from_state = ticket.state
    if dry_run:
        return planned(ticket, Ticket.State.IGNORED, BoardAction.IGNORED_CLOSED, reason)
    ticket.ignore()
    ticket.save()
    logger.info("Board reconcile resolved ticket %s %s → ignored (%s)", ticket.pk, from_state, reason)
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=ticket.state,
        action=BoardAction.IGNORED_CLOSED,
        reason=reason,
        applied=True,
    )


def _advance_to_delivered(ticket: "Ticket", *, dry_run: bool) -> BoardTransition | None:
    """Walk one post-ship ticket toward DELIVERED, or report the intent under *dry_run*.

    Delegates to ``Ticket.advance_to_delivered`` so this path keeps the atomic-per-step,
    refusal-safe semantics: a mid-chain gate refusal leaves the earlier steps persisted
    and is reported as the landing state rather than escaping as an exception.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    reason = "upstream issue done"
    if dry_run:
        return planned(ticket, Ticket.State.DELIVERED, BoardAction.ADVANCED_DELIVERED, reason)
    if ticket.state in {Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED}:
        from teatree.core.gates.merge_evidence_gate import (  # noqa: PLC0415 — ORM import after app setup
            record_confirmed_forge_merge,
        )

        record_confirmed_forge_merge(ticket)
    outcome = ticket.advance_to_delivered()
    if outcome.refused:
        logger.warning("Board reconcile refused on ticket %s (%s): %s", ticket.pk, ticket.issue_url, outcome.error)
    elif not outcome.advanced:
        return None
    else:
        logger.info(
            "Board reconcile advanced ticket %s %s → %s (%s)", ticket.pk, outcome.from_state, outcome.to_state, reason
        )
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=outcome.from_state,
        to_state=outcome.to_state,
        action=BoardAction.REFUSED if outcome.refused else BoardAction.ADVANCED_DELIVERED,
        reason=reason,
        applied=outcome.advanced,
        error=outcome.error or "",
    )
