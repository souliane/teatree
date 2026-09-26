"""Persist agent-kind dispatch actions as Ticket + Task DB rows.

The statusline is for *displaying*; the DB is for *orchestrating*. When
the tick produces a ``DispatchAction(kind="agent", …)`` — a reviewer
request, an auto-start orchestrator, etc. — this module translates the
action into the appropriate ``Ticket`` and initial ``Task`` rows. The
``/loop`` slot then reads pending Tasks via the loop CLI and spawns
sub-agents in-session.

Idempotency lives here, not at the scanner layer: scanners may emit on
every tick (the ``ReviewerPrsScanner`` cache only updates when the
review task actually completes), but a duplicate enqueue is a no-op
because we look up the existing Ticket+Task before creating new rows.
"""

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.intake.ticket_kind_classification import TicketOrigin, classify_ticket_kind
from teatree.core.models import ImplementedIssueMarker, RedMrFixAttempt, Task, Ticket
from teatree.core.models.auto_implement import mark_auto_implement
from teatree.loop.dispatch import DispatchAction
from teatree.loop.dispatch_gates import claim_red_mr_fix, fix_kind_of
from teatree.loop.dispatch_tables import PERSISTED_AT_SOURCE_ZONES
from teatree.loop.persistence_phase_task import create_phase_task, has_open_task

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)


def persist_agent_actions(
    actions: list[DispatchAction],
    *,
    errors: dict[str, str] | None = None,
) -> list[Task]:
    """Translate ``kind="agent"`` actions into DB rows; return the newly created Tasks.

    The DB is the dispatch queue: every agent zone a ``dispatch_*`` path emits
    is a COMPLETE executor contract here — routed by ``zone`` to a per-zone
    handler (``_ZONE_HANDLERS``) that creates the ``Ticket`` + initial ``Task``
    the ``/loop`` slot then dispatches. A ``pending_task`` re-emission (carrying
    the existing row's ``task_id``) is persisted-by-construction, so it is a
    deliberate no-op.

    Fail-loud (#1 blocker): an agent zone with no handler that is NOT a
    persisted-at-source zone is a *dropped dispatch* — it records
    ``errors["persist:<zone>"]`` (rendered in ``action_needed``) instead of a
    silent ``logger.debug``, so a new producer with no consumer is visible.
    ``errors`` is the ``TickReport.errors`` sink threaded from
    ``tick_recovery._persist_agent_dispatches``.
    """
    created: list[Task] = []
    for action in actions:
        if action.kind != "agent":
            continue
        task = _persist_one(action, errors)
        if task is not None:
            created.append(task)
    return created


def _persist_one(action: DispatchAction, errors: dict[str, str] | None) -> Task | None:
    # A ``pending_task`` re-emission carries the existing row's ``task_id``: the
    # Task is already in the DB (persisted at source), so persisting is a
    # deliberate no-op. Checked before the handler lookup because a persisted
    # zone (e.g. ``t3:coder``) overlaps a handler zone (skill-drift → t3:coder):
    # only the NEW-work action (no ``task_id``) must reach the handler.
    if "task_id" in action.payload:
        return None
    handler = _ZONE_HANDLERS.get(action.zone)
    if handler is None:
        if action.zone not in PERSISTED_AT_SOURCE_ZONES:
            _record_persist_error(errors, action.zone, f"unhandled agent dispatch zone {action.zone!r}")
            logger.error("No persistence handler for agent zone %r (detail=%r)", action.zone, action.detail)
        return None
    try:
        return handler(action)
    except Exception as exc:
        logger.exception("Persistence handler for zone %r failed", action.zone)
        _record_persist_error(errors, action.zone, f"{type(exc).__name__}: {exc}")
        return None


def _record_persist_error(errors: dict[str, str] | None, zone: str, detail: str) -> None:
    if errors is not None:
        errors[f"persist:{zone}"] = detail


def _owning_overlay(url: str, scan_tag: str) -> str:
    """Resolve the overlay that *owns* ``url``, preferring URL inference.

    The dispatch payload carries ``overlay`` = the *scanning* overlay's tag
    (``loop/tick.py`` injects ``job.overlay``), not necessarily the overlay
    whose workspace repos own the URL.  When a multi-overlay tick's
    reviewer/orchestrator scanner surfaces an issue/PR owned by a *different*
    overlay, persisting the scan tag leaks the ticket into the scanning
    overlay's statusline zone — and because ``overlay`` is then non-empty,
    ``Ticket.save()`` never runs ``_infer_overlay()`` to correct it (#806,
    incomplete #743).

    Inference (``infer_overlay_for_url``) is the single source of truth; the
    scan tag is only a fallback for the inconclusive case (URL owned by no
    registered overlay, e.g. a host neither overlay declares).
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415 — deferred: loaded at tick time

    return infer_overlay_for_url(url) or scan_tag


def _reconcile_existing_overlay(ticket: Ticket, *, created: bool) -> None:
    """Correct an already-persisted ticket whose overlay no longer matches.

    ``get_or_create`` may resolve a pre-existing row whose ``overlay`` was
    written from a stale/wrong scan tag.  Re-infer from ``issue_url`` and
    persist the correction.  ``apply_inferred_overlay`` keeps the #743
    invariant: an inconclusive (empty) inference never blanks a value that
    is already set, so a host no overlay declares is left as-is.
    """
    if created:
        return
    ticket.reconcile_overlay()


def _handle_reviewer(action: DispatchAction) -> Task | None:
    """Reviewer-requested PR → Ticket(role=reviewer) + Task(phase=reviewing)."""
    from teatree.loop.persistence_reviewer import handle_reviewer  # noqa: PLC0415 — the leaf imports this hub

    return handle_reviewer(action)


def _handle_orchestrator(action: DispatchAction) -> Task | None:
    """Auto-start assigned issue → Ticket(role=author) + Task(phase=planning).

    Only fires for ``issue_intake.admitted`` signals that carry ``auto_start=True`` (the
    dispatcher already filtered). The scheduled planning task is then dispatched per-phase
    by the loop — ``pending_task`` signals route directly to the phase's own agent, not
    through ``t3:orchestrator``.

    PLANNING, not coding (souliane/teatree#4578): the plan-before-dispatch gate refuses
    ``t3:coder`` on a ticket with no ``PlanArtifact``, so intake's coding task failed
    ``plan_missing`` hourly and nothing scheduled the planning that would satisfy it.
    ``Ticket.plan()`` schedules the coding one rung later, where the gate is satisfied.
    """
    payload = action.payload
    if payload.get("auto_start") is not True:
        return None
    issue_url = str(payload.get("issue_url") or payload.get("url") or "")
    if not issue_url:
        logger.debug("Skipping t3:orchestrator action with no issue_url: %r", action.detail)
        return None
    scan_tag = str(payload.get("overlay") or "")
    ticket, created = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"overlay": _owning_overlay(issue_url, scan_tag), "role": Ticket.Role.AUTHOR},
    )
    _reconcile_existing_overlay(ticket, created=created)
    _link_claimed_marker(ticket, issue_url)
    # #748: a loop/coordinator-built ticket must have a durable phase-
    # attestation session even when scheduling below is skipped (role
    # mismatch / not NOT_STARTED / open task), so the shipping gate can
    # reconcile real work instead of fail-closing on "no session".
    ticket.ensure_session()
    if ticket.role != Ticket.Role.AUTHOR:
        logger.debug("Ticket %s for %s has role=%s; not scheduling planning", ticket.pk, issue_url, ticket.role)
        return None
    if has_open_task(ticket, phase="coding") or ticket.state != Ticket.State.NOT_STARTED:
        return None
    # Kept as the fallback edge: ``code_direct`` is conditioned on this marker and is the
    # only transition advancing a coding completion that lands before PLANNED (#10).
    mark_auto_implement(ticket)
    return ticket.begin_planning()


def _link_claimed_marker(ticket: Ticket, issue_url: str) -> None:
    """Link the claimed marker to its new Ticket and advance it to ``TICKET_CREATED``.

    This handler is the intended (and, before now, the only) writer of the
    ``TICKET_CREATED`` state — the claim path leaves the marker ``DISPATCHED``.
    Every ``State.terminal()`` marker is excluded so a re-tick on an issue already
    completed, abandoned or declined can never resurrect one into the budget.
    """
    ImplementedIssueMarker.objects.filter(issue_url=issue_url).exclude(
        state__in=ImplementedIssueMarker.State.terminal()
    ).update(ticket=ticket, state=ImplementedIssueMarker.State.TICKET_CREATED)


def _get_or_create_ticket(
    url: str,
    *,
    role: str,
    overlay: str,
    extra: "TicketExtra | None" = None,
    kind: Ticket.Kind = Ticket.Kind.FEATURE,
) -> tuple[Ticket, bool]:
    """``get_or_create`` a ticket keyed on ``url`` + reconcile its overlay.

    The shared ticket-row primitive for the correction-zone handlers below
    (debug / codex / red-card / e2e / skill-drift / answerer). ``overlay`` is the
    already-resolved owning overlay (a real forge URL resolves via
    :func:`_owning_overlay`; a synthetic ``<scheme>://…`` key uses the scan tag).
    ``_reconcile_existing_overlay`` re-infers a pre-existing row from its
    ``issue_url`` and never blanks a set value (#743), so a synthetic key whose
    inference is empty is left untouched.

    ``kind`` (#17) is stamped only on a freshly-created row (``defaults``); the
    correction-origin handlers pass ``FIX`` so the fix-record DoD gate and S2
    defect-escape signal actually see the corrective work they are meant to.
    """
    ticket, created = Ticket.objects.get_or_create(
        issue_url=url,
        defaults={"overlay": overlay, "role": role, "extra": extra or {}, "kind": kind},
    )
    _reconcile_existing_overlay(ticket, created=created)
    return ticket, created


def _handle_orchestrator_zone(action: DispatchAction) -> Task | None:
    """Route the shared ``t3:orchestrator`` zone by payload shape.

    Two distinct signals dispatch to this one zone: the auto-start kickoff
    (``issue_intake.admitted``, carrying
    ``auto_start``) and the RED CARD corrective signal (carrying ``row_id`` with
    NO ``auto_start``). The RED CARD path was silently dropped inside
    ``_handle_orchestrator`` (its ``auto_start is not True`` guard, #1 blocker),
    so it gets its OWN handler here rather than sharing the auto-start body.
    """
    payload = action.payload
    if payload.get("row_id") and payload.get("auto_start") is not True:
        return _handle_red_card(action)
    return _handle_orchestrator(action)


def _handle_red_card(action: DispatchAction) -> Task | None:
    """RED CARD signal → author ticket + corrective ``coding`` task (#1130).

    Stamps the ``RedCardSignal`` row id into ``ticket.extra`` so the corrective
    agent can identify the upstream teatree gap, file the enforcement issue, and
    record it back via ``RedCardSignal.link_issue``. Keyed on a synthetic
    ``redcard://signal/<row_id>`` url so re-observing the same signal is
    idempotent (one corrective ticket per red card).
    """
    payload = action.payload
    row_id = payload.get("row_id")
    if not row_id:
        logger.debug("Skipping red_card action with no row_id: %r", action.detail)
        return None
    ticket, _created = _get_or_create_ticket(
        f"redcard://signal/{row_id}",
        role=Ticket.Role.AUTHOR,
        overlay=str(payload.get("overlay") or ""),
        extra={
            "red_card_signal_id": row_id,
            "red_card_signal_kind": str(payload.get("signal_kind") or ""),
            "red_card_signal_text": str(payload.get("signal_text") or ""),
            "red_card_offending_text": str(payload.get("offending_message_text") or ""),
        },
        kind=classify_ticket_kind(origin=TicketOrigin.CORRECTION),
    )
    if ticket.role != Ticket.Role.AUTHOR or has_open_task(ticket, phase="coding"):
        return None
    # Intentionally NOT gated by plan_currency (SELFCATCH-3): a redcard:// synthetic
    # ticket carries no PlanArtifact, so the adequacy/currency gate would false-positive.
    return create_phase_task(
        ticket,
        phase="coding",
        agent_id="red-card",
        reason=(
            "Auto-scheduled RED CARD corrective action — identify the upstream teatree gap, "
            "file the enforcement issue, and record it via RedCardSignal.link_issue"
        ),
    )


#: The remedy each un-mergeable condition's debugging task is scheduled for. The
#: conflict wording carries the never-rebase constraint into the task itself, so the
#: agent that picks it up does not have to re-derive it.
_FIX_REASON_BY_KIND: dict[str, str] = {
    RedMrFixAttempt.Kind.CI_RED: "Auto-scheduled red-MR fix — debug {pr_url}",
    RedMrFixAttempt.Kind.MERGE_CONFLICT: (
        "Auto-scheduled merge-conflict fix — resolve {pr_url} by MERGING the target branch "
        "into the head branch, never by rebasing"
    ),
    # Our own MR, so the finding is IMPLEMENTED rather than commented on: read the
    # unresolved discussions and the recorded review findings, fix them on the MR's
    # own branch, run the tests, push. Escalate only if the fix cannot proceed.
    RedMrFixAttempt.Kind.REVIEW_FINDINGS: (
        "Auto-scheduled review-findings fix — read the unresolved review findings on {pr_url}, "
        "IMPLEMENT them on that MR's own branch, run the tests and push. Never answer a finding "
        "on our own MR with another comment; escalate only if the fix cannot proceed"
    ),
}


def _handle_debug(action: DispatchAction) -> Task | None:
    """Un-mergeable own PR (``my_pr.failed`` / ``my_pr.conflicted``) → author ticket + ``debugging`` task.

    The ``RedMrFixAttempt`` idempotency claim (``claim_red_mr_fix``) rides the
    SAME atomic block that creates the Task, so a dropped/failed persist rolls
    the claim back and the next tick retries — the marker can no longer be burned
    before the fix ran (#1 blocker). A role conflict returns before the claim, so
    it is never touched. SIG-2 hardens WHAT is claimed (real sha / sentinel), and
    the payload's ``fix_kind`` selects both the ledger slot and the remedy the task
    is scheduled for.
    """
    payload = action.payload
    pr_url = str(payload.get("pr_url") or payload.get("url") or "")
    if not pr_url:
        logger.debug("Skipping t3:debug action with no pr_url: %r", action.detail)
        return None
    with transaction.atomic():
        ticket, _created = _get_or_create_ticket(
            pr_url,
            role=Ticket.Role.AUTHOR,
            overlay=_owning_overlay(pr_url, str(payload.get("overlay") or "")),
            kind=classify_ticket_kind(origin=TicketOrigin.CORRECTION),
        )
        if ticket.role != Ticket.Role.AUTHOR or has_open_task(ticket, phase="debugging"):
            return None
        if not claim_red_mr_fix(payload):
            return None
        return create_phase_task(
            ticket,
            phase="debugging",
            agent_id="debug",
            reason=_FIX_REASON_BY_KIND[fix_kind_of(payload)].format(pr_url=pr_url),
        )


def _handle_codex_review(action: DispatchAction) -> Task | None:
    """Codex auto-review dispatch → reviewer ticket + variant-encoded task (#1254).

    The ``CodexReviewMarker`` claim rides the SAME atomic block that creates the
    Task (#1 blocker): the scanner now emits unconditionally, so persistence owns
    the per-SHA idempotency and a dropped persist rolls the marker back. The
    review VARIANT is the dispatch zone (``codex:review`` /
    ``codex:adversarial-review``); the Task's PHASE encodes it so the /loop slot
    resolves the matching ``/codex:*`` slash-command agent directly.
    """
    from teatree.core.models.codex_review_marker import CodexReviewMarker  # noqa: PLC0415 — lazy: codex path only

    payload = action.payload
    pr_url = str(payload.get("pr_url") or payload.get("url") or "")
    slug = str(payload.get("slug") or "")
    pr_id = payload.get("pr_id")
    head_sha = str(payload.get("head_sha") or "")
    if not pr_url or not slug or not isinstance(pr_id, int) or not head_sha:
        logger.debug("Skipping codex-review action with incomplete payload: %r", action.detail)
        return None
    variant = action.zone
    phase = "codex_adversarial_reviewing" if variant == "codex:adversarial-review" else "codex_reviewing"
    with transaction.atomic():
        ticket, _created = _get_or_create_ticket(
            pr_url,
            role=Ticket.Role.REVIEWER,
            overlay=_owning_overlay(pr_url, str(payload.get("overlay") or "")),
            extra={"reviewed_sha": head_sha, "codex_variant": variant},
        )
        if ticket.role != Ticket.Role.REVIEWER or has_open_task(ticket, phase=phase):
            return None
        marker = CodexReviewMarker.claim(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            overlay=str(payload.get("overlay") or ""),
            variant=variant,
        )
        if marker is None:
            return None
        return create_phase_task(
            ticket,
            phase=phase,
            agent_id="codex-review",
            reason=f"Auto-scheduled codex review ({variant}) — {pr_url}",
        )


def _handle_e2e_fix(action: DispatchAction) -> Task | None:
    """Failed-E2E post (``e2e.failure_detected``) → author ticket + ``e2e`` task (#1295 cap E).

    Emission is deduped by the scanner's own ``ScannedFailedE2E`` ledger, so this
    handler carries no marker of its own. Keyed on a synthetic
    ``e2e-failure://<overlay>/<spec>`` url; the open-``e2e``-task check prevents a
    duplicate fix while one is in flight.
    """
    payload = action.payload
    spec = str(payload.get("spec") or "")
    if not spec:
        logger.debug("Skipping e2e-fix action with no spec: %r", action.detail)
        return None
    overlay = str(payload.get("skill_overlay") or payload.get("overlay") or "")
    ticket, _created = _get_or_create_ticket(
        f"e2e-failure://{overlay}/{spec}",
        role=Ticket.Role.AUTHOR,
        overlay=overlay,
        extra={"e2e_spec": spec, "e2e_test_title": str(payload.get("test_title") or "")},
        kind=classify_ticket_kind(origin=TicketOrigin.CORRECTION),
    )
    if ticket.role != Ticket.Role.AUTHOR or has_open_task(ticket, phase="e2e"):
        return None
    return create_phase_task(
        ticket,
        phase="e2e",
        agent_id="e2e-fix",
        reason=f"Auto-scheduled E2E fix — {spec}",
    )


def _handle_skill_drift(action: DispatchAction) -> Task | None:
    """Skill-drift finding (``skill_drift_detected``) → author ticket + ``coding`` task (#1295 cap H).

    Emission is deduped by the scanner's own ``AssessFinding`` ledger. Keyed on a
    synthetic ``skill-drift://<repo>/<file>`` url so one drift finding maps to one
    corrective coding ticket.
    """
    payload = action.payload
    repo = str(payload.get("repo") or "")
    file_path = str(payload.get("file_path") or payload.get("path") or "")
    if not repo or not file_path:
        logger.debug("Skipping skill-drift action with incomplete payload: %r", action.detail)
        return None
    ticket, _created = _get_or_create_ticket(
        f"skill-drift://{repo}/{file_path}",
        role=Ticket.Role.AUTHOR,
        overlay=str(payload.get("overlay") or ""),
        extra={
            "drift_repo": repo,
            "drift_file": file_path,
            "drift_fingerprint": str(payload.get("finding_fingerprint") or ""),
        },
        kind=classify_ticket_kind(origin=TicketOrigin.CORRECTION),
    )
    if ticket.role != Ticket.Role.AUTHOR or has_open_task(ticket, phase="coding"):
        return None
    # Intentionally NOT gated by plan_currency (SELFCATCH-3): a t3:coder skill-drift
    # synthetic ticket carries no PlanArtifact, so the currency gate would false-positive.
    return create_phase_task(
        ticket,
        phase="coding",
        agent_id="skill-drift",
        reason=f"Auto-scheduled skill-drift fix — {file_path}",
    )


def _handle_answerer(action: DispatchAction) -> Task | None:
    """Inbound question (``incoming_event.task_needed`` answering) → author ticket + ``answering`` task (#670).

    Keyed on a synthetic ``answer://event/<event_id>`` url (the inbound event has
    no forge URL) so re-observing the same event is idempotent.
    """
    payload = action.payload
    event_id = payload.get("event_id")
    if not event_id:
        logger.debug("Skipping answerer action with no event_id: %r", action.detail)
        return None
    ticket, _created = _get_or_create_ticket(
        f"answer://event/{event_id}",
        role=Ticket.Role.AUTHOR,
        overlay=str(payload.get("overlay") or ""),
        extra={"answer_event_id": event_id, "answer_detail": str(payload.get("detail") or "")},
    )
    if ticket.role != Ticket.Role.AUTHOR or has_open_task(ticket, phase="answering"):
        return None
    return create_phase_task(
        ticket,
        phase="answering",
        agent_id="answerer",
        reason="Auto-scheduled answer — respond to the inbound question",
    )


#: The COMPLETE executor contract: every non-``pending_task`` agent zone a
#: ``dispatch_*`` path emits maps to a handler that creates its Ticket + Task.
#: ``tests/conformance/test_registry_parity.py`` asserts
#: ``AGENT_ZONES == set(_ZONE_HANDLERS) | PERSISTED_AT_SOURCE_ZONES`` so a new
#: producer with no consumer fails CI instead of silently dropping the dispatch.
_ZONE_HANDLERS = {
    "t3:reviewer": _handle_reviewer,
    "t3:orchestrator": _handle_orchestrator_zone,
    "t3:debug": _handle_debug,
    "t3:e2e": _handle_e2e_fix,
    "t3:coder": _handle_skill_drift,
    "t3:answerer": _handle_answerer,
    "codex:review": _handle_codex_review,
    "codex:adversarial-review": _handle_codex_review,
}

#: The ``(role, phase)`` pairs the handlers above write rows on — asserted a
#: subset of ``SUBAGENT_BY_PHASE`` by the parity test so every persisted row has
#: a claimer that can dispatch it (a row no phase agent can pick up fails CI).
_HANDLER_TARGET_PHASES: frozenset[tuple[str, str]] = frozenset(
    {
        ("reviewer", "reviewing"),  # _handle_reviewer
        ("author", "planning"),  # _handle_orchestrator
        ("author", "coding"),  # _handle_red_card / _handle_skill_drift
        ("author", "debugging"),  # _handle_debug
        ("author", "e2e"),  # _handle_e2e_fix
        ("author", "answering"),  # _handle_answerer
        ("reviewer", "codex_reviewing"),  # _handle_codex_review (standard)
        ("reviewer", "codex_adversarial_reviewing"),  # _handle_codex_review (adversarial)
    },
)


__all__ = ["persist_agent_actions"]
