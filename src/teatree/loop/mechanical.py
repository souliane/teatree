"""Mechanical action handlers — inline ticket transitions executed during a tick.

Each handler receives an ``ActionPayload`` dict and mutates the DB directly.
Called by ``tick._execute_mechanical`` after dispatch, before statusline render.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from django_fsm import can_proceed

from teatree.core.review.author_trust import classify_author
from teatree.loop.dispatch import ActionPayload
from teatree.loop.mechanical_db_backup import run_db_backup
from teatree.loop.mechanical_local_stack import drain_stack_queue_item, reap_idle_stack
from teatree.loop.mechanical_resources import free_resources
from teatree.loop.mechanical_snapshot_warmer import refresh_snapshot
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models.task import Task

logger = logging.getLogger(__name__)


def payload_author_untrusted_public(payload: ActionPayload) -> bool:
    """True iff the payload's ``url`` + ``author`` is an untrusted PUBLIC-repo author (#1773).

    The shared author-trust classifier a mechanical handler consults before
    treating a non-self-authored signal as benign. Reuses the SAME
    :func:`classify_author` the keystone and the three reviewing scanners use,
    so the four cannot drift. Returns False when the payload carries no explicit
    author or no resolvable PR url — the legacy signals that omit the author
    were already verified self-authored by the emitting scanner, so the belt
    only acts on an EXPLICIT author it can independently classify.
    """
    author = str(payload.get("author") or "")
    if not author:
        return False
    ref = pr_ref_from_url(str(payload.get("url") or payload.get("mr_url") or ""))
    if ref is None:
        return False
    return classify_author(ref.slug, author, host_kind=ref.host_kind).untrusted


def ignore_disposed_ticket(payload: ActionPayload) -> None:
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)
    # #1087: the disposition signal re-emits every tick while the ticket
    # stays IGNORED (its PR keystone-merged, issue auto-closed). Driving
    # ``ignore`` from ``ignored`` is not a valid FSM transition — guard so
    # the already-satisfied desired state is a silent no-op, not every-tick
    # ``TransitionNotAllowed`` noise.
    if not can_proceed(ticket.ignore):
        return
    ticket.ignore()
    ticket.save()
    logger.info("Auto-ignored ticket %s (reason: %s)", ticket_id, payload.get("reason", "?"))


def complete_ticket(payload: ActionPayload) -> None:
    """Transition a ticket from its current post-ship state toward delivered.

    FSM path: shipped → request_review → mark_merged → retrospect.
    """
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)

    if ticket.state == "shipped":
        ticket.request_review()
        ticket.save()
    if ticket.state == "in_review":
        ticket.mark_merged()
        ticket.save()
    if ticket.state == "merged":
        ticket.retrospect()
        ticket.save()


def reopen_ticket(payload: ActionPayload) -> None:
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)
    # #1087: same re-emit hazard as ``ignore_disposed_ticket`` — a reopen
    # signal that persists across ticks would drive ``reopen`` from the
    # already-STARTED target state, raising every-tick ``TransitionNotAllowed``.
    if not can_proceed(ticket.reopen):
        return
    ticket.reopen()
    ticket.save()
    logger.info("Auto-reopened ticket %s (was %s, draft MRs detected)", ticket_id, payload.get("ticket_state", "?"))


def reviewer_task_orphaned(payload: ActionPayload) -> None:
    """Complete every open reviewing task on the orphaned reviewer ticket (#998).

    The scanner emits this signal ONLY after ``host.get_pr_open_state``
    confirmed the PR is genuinely MERGED or CLOSED (#1074) — never on mere
    absence from the reviewer-assignment scan. Without this sweep the
    PENDING task for a truly-merged PR lingers forever, surfacing on every
    ``pending-spawn`` and dispatching a reviewer sub-agent for nothing.

    The handler is intentionally narrow: it operates by ticket id and only
    completes tasks in ``phase=reviewing`` with non-terminal status. Other
    tasks on the same ticket (or other phases) are untouched. Best-effort —
    a missing ticket or already-completed tasks no-op silently.
    """
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    try:
        ticket = ticket_model.objects.get(pk=ticket_id)
    except ticket_model.DoesNotExist:
        return
    completed = _complete_open_reviewing_tasks(ticket)
    if completed:
        logger.info(
            "Auto-completed %d orphaned reviewing task(s) on ticket %s (PR %s confirmed merged/closed)",
            completed,
            ticket_id,
            payload.get("url", "?"),
        )


def reviewer_task_self_authored(payload: ActionPayload) -> None:
    """Complete every open reviewing task on a SELF-AUTHORED MR's reviewer ticket (#1321).

    The scanner emits this signal when ``list_review_requested_prs``
    surfaces an MR the user authored (under any of their configured
    identities). Own MRs route to coder/debugger + a colleague
    review-request — never a ``t3:reviewer`` sub-agent. Without this sweep
    a reviewing task created for a self-authored OPEN MR (the orphan sweep
    only reaps MERGED/CLOSED PRs) lingers forever and re-dispatches a
    self-review every ``pending-spawn``.

    Narrow and best-effort, mirroring :func:`reviewer_task_orphaned`: by
    ticket id, only ``phase=reviewing`` non-terminal tasks; a missing
    ticket no-ops silently.
    """
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    if payload_author_untrusted_public(payload):
        # #1773: a self-authored signal must never silently close the reviewing
        # task when the author is an untrusted identity on a PUBLIC repo — that
        # PR needs an adversarial review, not a "no self-review" skip. Refuse
        # the auto-complete (the keystone refuses the merge too — invariant 8).
        logger.warning(
            "reviewer_task_self_authored: refusing to auto-close reviewing task on ticket %s — "
            "untrusted author on a public repo (%s) must get an adversarial review",
            ticket_id,
            payload.get("url", "?"),
        )
        return
    try:
        ticket = ticket_model.objects.get(pk=ticket_id)
    except ticket_model.DoesNotExist:
        return
    completed = _complete_open_reviewing_tasks(ticket)
    if completed:
        logger.info(
            "Auto-completed %d reviewing task(s) on ticket %s (self-authored MR %s — no self-review)",
            completed,
            ticket_id,
            payload.get("url", "?"),
        )


def _complete_open_reviewing_tasks(ticket: object) -> int:
    """Complete every non-terminal ``phase=reviewing`` task on *ticket*; return the count."""
    from teatree.core.models.task import Task  # noqa: PLC0415

    open_tasks = Task.objects.pending_in_phase("reviewing").filter(ticket=ticket)
    completed = 0
    for task in open_tasks:
        task.complete()
        completed += 1
    return completed


def task_completion(payload: ActionPayload) -> None:
    """Complete a swept teatree task whose artifact is terminal — RE-checking first (#129).

    The ``task_sweep`` scanner emits ``task.completion_detected`` after
    ``is_issue_done`` returned True for the task's issue. Because dispatch runs
    after every scanner and the artifact could (in principle) re-open between
    the scan and this handler, the handler re-verifies the terminal state
    against the live code host before advancing the FSM — never auto-complete
    on a stale read. Best-effort and idempotent: a missing task, an
    already-terminal task, or a host that can no longer confirm the issue is
    done all no-op silently rather than crash the tick.
    """
    from teatree.core.models.task import Task  # noqa: PLC0415

    task_id = payload.get("task_id")
    if task_id is None:
        return
    try:
        task = Task.objects.select_related("ticket").get(pk=task_id)
    except Task.DoesNotExist:
        return
    if task.status in Task.Status.terminal():
        return
    if not _artifact_still_terminal(task):
        logger.info("task_completion: task %s artifact no longer terminal — skipping completion", task_id)
        return
    task.complete()
    logger.info("Auto-completed task %s (artifact confirmed terminal: %s)", task_id, payload.get("issue_url", "?"))


def _artifact_still_terminal(task: "Task") -> bool:
    """Re-verify the task's issue is done via the live code host (fail-CLOSED).

    Returns True only when the overlay's ``is_issue_done`` confirms the issue
    on a fresh fetch. Any uncertainty — no host, fetch error, error payload —
    returns False so the handler does NOT complete the task (the opposite of
    the scanner's fail-OPEN-to-orphaned: at the *completion* gate, uncertainty
    must block the irreversible action, not permit it).
    """
    from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    issue_url = task.ticket.issue_url
    if not issue_url:
        return False
    try:
        overlay = get_overlay(task.ticket.overlay or None)
        host = get_code_host_for_url(overlay, issue_url)
    except Exception:
        logger.exception("task_completion: could not resolve code host for %s", issue_url)
        return False
    if host is None:
        return False
    try:
        issue_data = host.get_issue(issue_url)
    except Exception:  # noqa: BLE001 — any host error fails CLOSED (no completion), never crashes the tick.
        logger.warning("task_completion: re-check fetch failed for %s", issue_url)
        return False
    if not isinstance(issue_data, dict) or "error" in issue_data:
        return False
    return bool(overlay.is_issue_done(issue_data))


def assign_gitlab_reviewer(payload: ActionPayload) -> None:
    """Append the user as reviewer on the MR carried by *payload* (#1295 cap B).

    Reads ``url`` and ``reviewer_username`` from the payload, resolves
    the active overlay's GitLab host, and calls
    :meth:`GitLabCodeHost.assign_reviewer` which preserves the existing
    reviewer list. Best-effort: any failure logs without raising so a
    Slack mention on a non-GitLab forge or a transient API hiccup
    cannot wedge the tick.
    """
    pr_url = str(payload.get("url") or payload.get("mr_url") or "")
    reviewer_username = str(payload.get("reviewer_username", ""))
    if not pr_url or not reviewer_username:
        return
    try:
        from teatree.backends.loader import get_code_host  # noqa: PLC0415
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        overlay = get_overlay(str(payload.get("overlay") or "") or None)
        host = get_code_host(overlay)
    except Exception:
        logger.exception("Could not resolve code host for cap-B assignment of %s", pr_url)
        return
    if host is None:
        logger.info("No code host resolved for cap-B assignment of %s", pr_url)
        return
    assign = getattr(host, "assign_reviewer", None)
    if assign is None or not callable(assign):
        logger.info("Code host has no assign_reviewer support for %s — skipping cap-B", pr_url)
        return
    try:
        ok = assign(pr_url=pr_url, username=reviewer_username)
    except Exception:
        logger.exception("Failed to assign %s as reviewer on %s", reviewer_username, pr_url)
        return
    if ok:
        logger.info("Assigned %s as reviewer on %s via Slack-mention pickup", reviewer_username, pr_url)
    else:
        logger.warning("assign_reviewer returned False for %s on %s", reviewer_username, pr_url)


_DISPOSITION_AUDIT_REASONS: dict[str, str] = {
    "already_shipped": "already shipped — a delivered ticket exists for this issue",
    "exact_duplicate": "exact duplicate of another open issue with the same title",
    "obsolete": "obsolete — every file path it references is gone from the repo",
}


def close_dead_issue(payload: ActionPayload) -> None:
    """Close a high-confidence DEAD issue with an audit-trail comment (#2122).

    The ``IssueDispositionScanner`` emits ``issue_disposition.close_candidate``
    only for issues carrying machine-checkable dead evidence; this handler
    resolves the code host for the issue URL and closes it. Idempotent: the
    backend ``close_issue`` is a no-op on an already-closed issue, so a re-tick
    on the same candidate does no harm. Best-effort — a missing URL, an
    unresolvable host, or a backend error logs without raising so the tick
    never wedges. The handler labels/closes only; it creates no Task or claim.
    """
    from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    issue_url = str(payload.get("url") or payload.get("issue_url") or "")
    if not issue_url:
        return
    reason = str(payload.get("reason", ""))
    try:
        overlay = get_overlay(str(payload.get("overlay") or "") or None)
        host = get_code_host_for_url(overlay, issue_url)
    except Exception:
        logger.exception("close_dead_issue: could not resolve code host for %s", issue_url)
        return
    if host is None:
        logger.info("close_dead_issue: no code host resolved for %s", issue_url)
        return
    audit = _DISPOSITION_AUDIT_REASONS.get(reason, reason or "machine-detected dead evidence")
    comment = f"Auto-closed by the issue-disposition scanner: {audit}."
    try:
        result = host.close_issue(issue_url=issue_url, comment=comment)
    except Exception:
        logger.exception("close_dead_issue: failed to close %s", issue_url)
        return
    if isinstance(result, dict) and "error" in result:
        logger.warning("close_dead_issue: backend refused to close %s (%s)", issue_url, result["error"])
        return
    logger.info("Auto-closed DEAD issue %s (reason: %s)", issue_url, reason or "?")


HANDLERS: dict[str, Callable[[ActionPayload], None]] = {
    "ticket_disposition": ignore_disposed_ticket,
    "ticket_completion": complete_ticket,
    "ticket_reopen": reopen_ticket,
    "reviewer_task_orphaned": reviewer_task_orphaned,
    "reviewer_task_self_authored": reviewer_task_self_authored,
    "assign_gitlab_reviewer": assign_gitlab_reviewer,
    "free_resources": free_resources,
    "task_completion": task_completion,
    "close_dead_issue": close_dead_issue,
    # #2190 idle-stack reaper + acquisition-queue drainer. The scanners only
    # flag candidates; the actual ``stop_services`` / ``start_services`` runs
    # here (re-verifying live state first, never an agent).
    "reap_idle_stack": reap_idle_stack,
    "drain_stack_queue_item": drain_stack_queue_item,
    # souliane/teatree#2949 snapshot warmer — restore+migrate+snapshot a
    # stale reference DB out-of-band from any ticket-critical-path provision.
    "refresh_snapshot": refresh_snapshot,
    # Directive #2 daily control-DB backup — snapshot the live control DB +
    # prune past the keep-last-N-days retention, off the tick.
    "run_db_backup": run_db_backup,
}
