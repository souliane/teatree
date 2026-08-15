"""Mechanical action handlers — inline ticket transitions executed during a tick.

Each handler receives an ``ActionPayload`` dict and mutates the DB directly.
Called by ``tick._execute_mechanical`` after dispatch, before statusline render.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from django_fsm import can_proceed

from teatree.core.review.author_trust import classify_author
from teatree.core.send_proxy import OutboundBlockedError, forge_from_url, route_forge_write
from teatree.loop.dispatch import ActionPayload
from teatree.loop.mechanical_ci_eval_heal import advance_ci_eval_heal
from teatree.loop.mechanical_db_backup import run_db_backup
from teatree.loop.mechanical_local_stack import drain_stack_queue_item, reap_idle_stack
from teatree.loop.mechanical_resources import free_resources
from teatree.loop.mechanical_snapshot_warmer import refresh_snapshot
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from teatree.core.backend_protocols import CodeHostBackend
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
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

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

    FSM path: shipped → request_review → mark_merged → retrospect. Delegates to
    ``Ticket.advance_to_delivered`` so this loop path shares the CLI's
    atomic-per-step + refusal-safe semantics: a mid-chain gate refusal (e.g. the
    merge-evidence gate on ``mark_merged``) stops the walk gracefully instead of
    escaping as an exception after a partial commit — the every-tick error the
    ungated cascade used to raise.
    """
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)

    result = ticket.advance_to_delivered()
    if result.refused:
        logger.info(
            "complete_ticket: ticket %s advance stopped at %s (%s → %s): %s",
            ticket_id,
            result.to_state,
            result.from_state,
            result.to_state,
            result.error,
        )


def reopen_ticket(payload: ActionPayload) -> None:
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

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

    The scanner emits this signal on either of two proofs, never on mere
    absence from the reviewer-assignment scan: ``host.get_pr_open_state``
    confirmed the PR is genuinely MERGED or CLOSED (#1074), or the local FSM
    already reached a terminal state (#1431). Without this sweep the PENDING
    task lingers forever, surfacing on every ``pending-spawn`` and dispatching
    a reviewer sub-agent for nothing.

    The two grounds are NOT interchangeable in a log (#3910): a terminal ticket
    on a still-OPEN PR is a correct reap, so crediting it to the forge-state
    proof reads as a forge bug and sends the operator hunting a phantom. The
    signal carries the ground it actually used in ``payload["reason"]``.

    The handler is intentionally narrow: it operates by ticket id and only
    completes tasks in ``phase=reviewing`` with non-terminal status. Other
    tasks on the same ticket (or other phases) are untouched. Best-effort —
    a missing ticket or already-completed tasks no-op silently.
    """
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    try:
        ticket = ticket_model.objects.get(pk=ticket_id)
    except ticket_model.DoesNotExist:
        return
    reason = payload.get("reason", "orphaned")
    completed = _complete_open_reviewing_tasks(
        ticket, skip_reason=f"the reviewing task was orphaned ({reason}), so no review ran"
    )
    if completed:
        logger.info(
            "Auto-completed %d orphaned reviewing task(s) on ticket %s (%s: %s)",
            completed,
            ticket_id,
            payload.get("url", "?"),
            payload.get("reason", "orphaned"),
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

    Narrower than :func:`reviewer_task_orphaned` in one way (#3910): a task the
    #68 auto-review dispatch armed is skipped. That premise — own MR means a
    colleague review-request — does not hold on a solo overlay, where the agent
    cold-reviewer IS the checker and the armed task is the only route to a
    merge. :func:`reviewer_task_orphaned` reaps it regardless, because a
    merged/closed PR makes even an armed review dead work.
    """
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

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
    completed = _complete_tasks(
        _open_reviewing_tasks(ticket).not_auto_review_armed(),
        skip_reason="the MR is self-authored, so no self-review ran",
    )
    if completed:
        logger.info(
            "Auto-completed %d reviewing task(s) on ticket %s (self-authored MR %s — no self-review)",
            completed,
            ticket_id,
            payload.get("url", "?"),
        )


def _open_reviewing_tasks(ticket: object) -> "QuerySet":
    """Every non-terminal ``phase=reviewing`` task on *ticket*."""
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return Task.objects.pending_in_phase("reviewing").filter(ticket=ticket)


def _complete_open_reviewing_tasks(ticket: object, *, skip_reason: str) -> int:
    """Complete every non-terminal ``phase=reviewing`` task on *ticket*; return the count."""
    return _complete_tasks(_open_reviewing_tasks(ticket), skip_reason=skip_reason)


def _complete_tasks(tasks: "QuerySet", *, skip_reason: str) -> int:
    """Complete each task, recording the attempt that says no review ran (#4308).

    A bare ``complete()`` leaves a reviewing row with ZERO attempts, which reads exactly
    like a review that ran and recorded nothing — so a PR held by a stale verdict looked
    reviewed every time this skip fired. The attempt is exit-0 because the skip is
    deliberate (the PR is dead, or self-authored on a lane with no self-review): failing it
    would feed the auto-repair sweep a "re-do this" signal for work nobody owes.
    """
    completed = 0
    for task in tasks:
        task.complete_with_attempt(result={"summary": f"no verdict reached: {skip_reason}"})
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
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.utils.url_slug import is_synthetic_loop_umbrella_url  # noqa: PLC0415 — deferred: tick-time import

    task_id = payload.get("task_id")
    if task_id is None:
        return
    try:
        task = Task.objects.select_related("ticket").get(pk=task_id)
    except Task.DoesNotExist:
        return
    if task.status in Task.Status.terminal():
        return
    # Defence in depth (#3706): a synthetic loop ticket anchors on the shared umbrella
    # issue, whose upstream closed state says nothing about whether the loop work is done.
    # The sweep already excludes these, so this signal should never carry one — never
    # artifact-complete it if one slips through.
    if is_synthetic_loop_umbrella_url(task.ticket.issue_url):
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
    from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: loaded at tick time, not import

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
        from teatree.backends.loader import get_code_host  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: loaded at tick time, not import

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


def _scrub_disposition_close_comment(host: "CodeHostBackend", issue_url: str, comment: str) -> str | None:
    """Return the close comment to post after the scanned forge-write seam, or ``None`` to skip.

    Matches the MCP ``<forge>_issue_close`` twin: the public-repo leak gate + the
    #117 send-proxy run BEFORE the backend close, so this loop-driven close never
    posts to a public forge on a laxer path than the MCP surface. A leak/blocked
    verdict — or any scrub failure — returns ``None`` (skip the close: never
    raise, never post unscanned) rather than wedging the tick.
    """
    try:
        return route_forge_write(
            forge=forge_from_url(issue_url),
            repo=host.repo_for_issue_url(issue_url),
            text=comment,
            action="issue_disposition_close",
            target=issue_url,
        )
    except OutboundBlockedError:
        logger.warning("close_dead_issue: close comment refused by the forge-write seam for %s — skipping", issue_url)
        return None
    except Exception:
        logger.exception("close_dead_issue: could not scrub the close comment for %s", issue_url)
        return None


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
    from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: loaded at tick time, not import

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
    raw_comment = f"Auto-closed by the issue-disposition scanner: {audit}."
    comment = _scrub_disposition_close_comment(host, issue_url, raw_comment)
    if comment is None:
        return
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
    # #3201 PR-3a CI-eval self-healing loop — advance every open heal session one
    # FSM step (dispatch / poll / GREEN / HALT+escalate). Observe-only, never a fix.
    "advance_ci_eval_heal": advance_ci_eval_heal,
}
