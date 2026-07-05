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

from teatree.core.models import Task, Ticket
from teatree.core.models.ticket_external_review import schedule_external_review
from teatree.loop.dispatch import DispatchAction
from teatree.loop.dispatch_gates import claim_red_mr_fix
from teatree.loop.dispatch_tables import PERSISTED_AT_SOURCE_ZONES

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
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

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
    payload = action.payload
    pr_url = str(payload.get("url") or "")
    if not pr_url:
        logger.debug("Skipping t3:reviewer action with no url: %r", action.detail)
        return None
    head_sha = str(payload.get("head_sha") or "")
    scan_tag = str(payload.get("overlay") or "")
    ticket, created = Ticket.objects.get_or_create(
        issue_url=pr_url,
        defaults={
            "overlay": _owning_overlay(pr_url, scan_tag),
            "role": Ticket.Role.REVIEWER,
            "extra": {"reviewed_sha": head_sha} if head_sha else {},
        },
    )
    _reconcile_existing_overlay(ticket, created=created)
    if ticket.role != Ticket.Role.REVIEWER:
        logger.debug(
            "Ticket %s exists with role=%s, not promoting to reviewer for PR %s",
            ticket.pk,
            ticket.role,
            pr_url,
        )
        return None
    if head_sha and (ticket.extra or {}).get("reviewed_sha") != head_sha:
        # #800 N3: canonical locked RMW — a concurrent pr_urls /
        # visual_qa writer no longer clobbers reviewed_sha.
        #
        # #959 defect 2: a SHA move invalidates any prior approval — drop
        # ``last_review_state`` in the same RMW so the
        # ``_already_reviewed_at_head`` dedup below does NOT suppress
        # review of the genuinely new revision (the recorded APPROVED
        # belonged to the old SHA).
        ticket.merge_extra(set_keys={"reviewed_sha": head_sha}, pop_keys=["last_review_state"])
    if _has_open_task(ticket, phase="reviewing"):
        return None
    if _already_reviewed_at_head(ticket, head_sha):
        # #959 defect 2: the MR was already independently reviewed AND
        # approved (e.g. an out-of-band review pass) at the CURRENT head
        # SHA. There is no *open* reviewing Task — the prior dedup
        # (open-task-only) re-enqueued review every tick (the live tasks
        # 49/50/51 for the already-approved SSO-mock MRs). A recorded
        # forge approval matching the current head is authoritative
        # "already reviewed"; a SHA move resets it via the mismatch path
        # above, so a genuinely new revision is still reviewed.
        logger.debug("PR %s already approved at head %s — not re-enqueuing review", pr_url, head_sha)
        return None
    return schedule_external_review(ticket)


def _already_reviewed_at_head(ticket: Ticket, head_sha: str) -> bool:
    """Has this PR a recorded terminal review observation at the current head?

    The dedup signal for an *out-of-band* review (one not driven by a
    loop reviewing Task, so there is no open/completed Task to key on) is
    the reviewer ticket's ``last_review_state``/``reviewed_sha`` pair —
    written by ``Ticket.mark_reviewed_externally`` / ``mark_review_no_action``
    and the ``ReviewerPrsScanner`` cache. A terminal state at the current
    head ⇒ the review already happened; re-enqueueing would duplicate it
    every tick. Two terminal states suppress: ``APPROVED`` (a genuine
    approving review — the existing #959 behaviour) and
    ``REVIEWED_NO_ACTION`` (the reviewer concluded there was nothing to
    post/approve on a bot MR — before #1077 there was no terminal state
    for this, so the reviewing task re-dispatched every Stop-hook pump
    forever). ``REVIEWED_NO_ACTION`` is intentionally *not* APPROVED so a
    future genuine review is never hidden; suppression is keyed on the
    head SHA, and a SHA move drops ``last_review_state`` (the #959 reset
    in ``_handle_reviewer``) so a new revision is still reviewed.

    A blank ``head_sha`` is treated as "cannot confirm parity" so review
    is NOT suppressed (fail-open — never silently skip a real review).
    """
    if not head_sha:
        return False
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    extra = ticket.extra or {}
    terminal = {ReviewState.APPROVED.value, ReviewState.REVIEWED_NO_ACTION.value}
    return extra.get("last_review_state") in terminal and extra.get("reviewed_sha") == head_sha


def _handle_orchestrator(action: DispatchAction) -> Task | None:
    """Auto-start assigned issue → Ticket(role=author) + Task(phase=coding).

    Only fires for ``assigned_issue.ready`` / ``issue_implementer.claimed``
    signals that carry ``auto_start=True`` (the dispatcher already filtered).
    The scheduled coding task is then dispatched per-phase by the loop —
    ``pending_task`` signals route directly to the phase's own agent, not
    through ``t3:orchestrator``.
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
    # #748: a loop/coordinator-built ticket must have a durable phase-
    # attestation session even when scheduling below is skipped (role
    # mismatch / not NOT_STARTED / open task), so the shipping gate can
    # reconcile real work instead of fail-closing on "no session".
    ticket.ensure_session()
    if ticket.role != Ticket.Role.AUTHOR:
        logger.debug(
            "Ticket %s for %s has role=%s; not scheduling coding",
            ticket.pk,
            issue_url,
            ticket.role,
        )
        return None
    if _has_open_task(ticket, phase="coding") or ticket.state != Ticket.State.NOT_STARTED:
        return None
    return ticket.schedule_coding()


def _has_open_task(ticket: Ticket, *, phase: str) -> bool:
    # #769 audit: match any accepted phase spelling (short verb or
    # gerund) via the shared SSOT helper, not a raw ``phase=phase``
    # filter that would miss a short-verb ``code`` task and let the
    # orchestrator create a duplicate.
    return Task.objects.pending_in_phase(phase).filter(ticket=ticket).exists()


def _get_or_create_ticket(
    url: str,
    *,
    role: str,
    overlay: str,
    extra: "TicketExtra | None" = None,
) -> tuple[Ticket, bool]:
    """``get_or_create`` a ticket keyed on ``url`` + reconcile its overlay.

    The shared ticket-row primitive for the correction-zone handlers below
    (debug / codex / red-card / e2e / skill-drift / answerer). ``overlay`` is the
    already-resolved owning overlay (a real forge URL resolves via
    :func:`_owning_overlay`; a synthetic ``<scheme>://…`` key uses the scan tag).
    ``_reconcile_existing_overlay`` re-infers a pre-existing row from its
    ``issue_url`` and never blanks a set value (#743), so a synthetic key whose
    inference is empty is left untouched.
    """
    ticket, created = Ticket.objects.get_or_create(
        issue_url=url,
        defaults={"overlay": overlay, "role": role, "extra": extra or {}},
    )
    _reconcile_existing_overlay(ticket, created=created)
    return ticket, created


def _create_phase_task(ticket: Ticket, *, phase: str, agent_id: str, reason: str) -> Task:
    """Create a fresh ``Session`` + initial ``Task`` for ``(ticket.role, phase)``.

    Mirrors ``ticket.schedule_coding`` / ``schedule_external_review`` for the
    phases those methods do not cover (``debugging``/``e2e``/``answering``/
    ``codex_reviewing``). ``Task.save`` routes a loop-dispatched ``(role, phase)``
    to INTERACTIVE under the default ``agent_runtime`` (the /loop slot is its
    dispatcher), so no explicit ``execution_target`` is set here.
    """
    from teatree.core.models.session import Session  # noqa: PLC0415 — lazy: avoids the models import cycle

    session = Session.objects.create(ticket=ticket, agent_id=agent_id)
    return Task.objects.create(ticket=ticket, session=session, phase=phase, execution_reason=reason)


def _handle_orchestrator_zone(action: DispatchAction) -> Task | None:
    """Route the shared ``t3:orchestrator`` zone by payload shape.

    Two distinct signals dispatch to this one zone: the auto-start kickoff
    (``assigned_issue.ready`` / ``issue_implementer.claimed``, carrying
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
    )
    if ticket.role != Ticket.Role.AUTHOR or _has_open_task(ticket, phase="coding"):
        return None
    # Intentionally NOT gated by plan_currency (SELFCATCH-3): a redcard:// synthetic
    # ticket carries no PlanArtifact, so the adequacy/currency gate would false-positive.
    return _create_phase_task(
        ticket,
        phase="coding",
        agent_id="red-card",
        reason=(
            "Auto-scheduled RED CARD corrective action — identify the upstream teatree gap, "
            "file the enforcement issue, and record it via RedCardSignal.link_issue"
        ),
    )


def _handle_debug(action: DispatchAction) -> Task | None:
    """Failing own PR (``my_pr.failed``) → author ticket + ``debugging`` task (#1295 cap D).

    The ``RedMrFixAttempt`` idempotency claim (``claim_red_mr_fix``) rides the
    SAME atomic block that creates the Task, so a dropped/failed persist rolls
    the claim back and the next tick retries — the marker can no longer be burned
    before the fix ran (#1 blocker). A role conflict returns before the claim, so
    it is never touched. SIG-2 hardens WHAT is claimed (real sha / sentinel).
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
        )
        if ticket.role != Ticket.Role.AUTHOR or _has_open_task(ticket, phase="debugging"):
            return None
        if not claim_red_mr_fix(payload):
            return None
        return _create_phase_task(
            ticket,
            phase="debugging",
            agent_id="debug",
            reason=f"Auto-scheduled red-MR fix — debug {pr_url}",
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
        if ticket.role != Ticket.Role.REVIEWER or _has_open_task(ticket, phase=phase):
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
        return _create_phase_task(
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
    )
    if ticket.role != Ticket.Role.AUTHOR or _has_open_task(ticket, phase="e2e"):
        return None
    return _create_phase_task(
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
    )
    if ticket.role != Ticket.Role.AUTHOR or _has_open_task(ticket, phase="coding"):
        return None
    # Intentionally NOT gated by plan_currency (SELFCATCH-3): a t3:coder skill-drift
    # synthetic ticket carries no PlanArtifact, so the currency gate would false-positive.
    return _create_phase_task(
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
    if ticket.role != Ticket.Role.AUTHOR or _has_open_task(ticket, phase="answering"):
        return None
    return _create_phase_task(
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
        ("author", "coding"),  # _handle_orchestrator / _handle_red_card / _handle_skill_drift
        ("author", "debugging"),  # _handle_debug
        ("author", "e2e"),  # _handle_e2e_fix
        ("author", "answering"),  # _handle_answerer
        ("reviewer", "codex_reviewing"),  # _handle_codex_review (standard)
        ("reviewer", "codex_adversarial_reviewing"),  # _handle_codex_review (adversarial)
    },
)


__all__ = ["persist_agent_actions"]
