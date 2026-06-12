"""Dispatch ``ScanSignal``s into actions: statusline notes or phase agents.

The dispatcher is the only place where the loop decides *what to do* with a
signal. Inline mechanical actions (lint fixes, n8n webhooks, statusline
notes) run here. Judgment calls are delegated to phase agents — the
dispatcher records the agent invocation as an ``"agent"`` action so the
runtime layer can spawn them via the standard Task tool.

The dispatcher does not invoke Claude; it only routes. The runtime that
hosts the loop (a Claude Code ``/loop`` slot) reads the action list and
spawns agents with the standard tool. This keeps unit tests deterministic.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from teatree.config import get_effective_settings
from teatree.core.phases import normalize_phase, subagent_for_phase
from teatree.loop.scanners.base import ScanSignal
from teatree.url_classify import find_pr_urls

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

ActionKind = Literal["statusline", "agent", "webhook", "mechanical"]
type ActionPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class DispatchAction:
    """One side-effect produced by the dispatcher for a tick."""

    kind: ActionKind
    zone: str  # statusline zone or agent name (depending on kind)
    detail: str
    payload: ActionPayload = field(default_factory=dict)


_AGENT_BY_KIND: dict[str, str] = {
    "reviewer_pr.new_sha": "t3:reviewer",
    "reviewer_pr.unreviewed": "t3:reviewer",
    "reviewer_pr.approval_dismissed": "t3:reviewer",
    # #1295 cap D: a failing MR/PR routes to the debug agent. Mirrored
    # into the statusline (via _DUAL_DISPATCH) so the user sees the red
    # MR even when the agent's dispatch is gated by the
    # ``RedMrFixAttempt`` ledger.
    "my_pr.failed": "t3:debug",
    # #1047: a Slack reaction/mention on an MR-bearing message routes to the
    # reviewer pipeline. The maker/checker boundary (BLUEPRINT §17.8) is
    # preserved because the reviewer agent runs as a separate dispatch from
    # whatever produced the Slack message.
    "slack.review_intent": "t3:reviewer",
    # #1130: a user RED CARD signal routes to the orchestrator, whose
    # corrective-action workflow identifies the upstream teatree gap and
    # files the enforcement issue. The signal payload carries the
    # ``RedCardSignal`` row id so the orchestrator can stamp the filed
    # issue URL back onto the row via ``RedCardSignal.link_issue``.
    "red_card.signal": "t3:orchestrator",
    # #1554: a newly-claimed auto-implement issue routes to the orchestrator
    # as a MAKER-side kickoff — it starts the normal maker pipeline for the
    # claimed issue. It issues no MergeClear and gains no new merge authority
    # (the §17.4 maker≠checker boundary is untouched). Mirrored into the
    # statusline below so the user sees the claimed issue without waiting on
    # the agent.
    "issue_implementer.claimed": "t3:orchestrator",
}

_STATUSLINE_ZONE_BY_KIND: dict[str, str] = {
    "my_pr.failed": "action_needed",
    "my_pr.draft_notes": "action_needed",
    "my_pr.open": "in_flight",
    "slack.mention": "action_needed",
    "slack.dm": "action_needed",
    "slack.review_intent": "action_needed",
    "red_card.signal": "action_needed",
    "assigned_issue.ready": "action_needed",
    # #1554: a claimed auto-implement issue is in-flight maker work the user
    # should see surfaced while the orchestrator picks it up.
    "issue_implementer.claimed": "action_needed",
    "ticket.active": "anchors",
    "ticket.disposition_candidate": "action_needed",
    "ticket.stale": "action_needed",
    # Reviewer-assigned PRs also dispatch to the t3:reviewer agent below,
    # but we mirror them into the statusline so the user sees what's
    # pending review without waiting on the agent to act.
    "reviewer_pr.new_sha": "action_needed",
    "reviewer_pr.unreviewed": "action_needed",
    "reviewer_pr.approval_dismissed": "action_needed",
    # Inbound webhook events (#669). `recorded` is the passive status-update
    # case — relegated to in_flight so a noisy CI doesn't flood action_needed.
    "incoming_event.alert": "action_needed",
    "incoming_event.task_needed": "action_needed",
    "incoming_event.merge_needed": "action_needed",
    "incoming_event.merge_blocked": "action_needed",
    "incoming_event.merge_escalation": "action_needed",
    "incoming_event.recorded": "in_flight",
    # #128 resource-pressure scanner — WARN-band advisories and any
    # cleanup failure surface in action_needed; the freeing itself routes
    # through the mechanical handler below. ``ram_kill_candidate`` is
    # statusline-only (never an agent) so a flagged process-kill remains a
    # user-visible advisory, not an autonomous action.
    "resource.pressure_warn": "action_needed",
    "resource.cleanup_failed": "action_needed",
    "resource.ram_kill_candidate": "action_needed",
    # #129 TODO-sweep — an orphaned (unverifiable) task surfaces for operator
    # review; the completion path routes through the mechanical handler below.
    "todo.orphaned": "action_needed",
    # Only the CI-green-gate skips reach here (see _is_self_update_ci_skip); a
    # clone wedged behind a red default branch must surface, not stay silent.
    "self_update.skipped": "action_needed",
    # Operator config gap, not per-MR bookkeeping — exempted from the drop below.
    "review_request_merge_react.missing_scope": "action_needed",
    # pr_sweep flag-level signals the scanner refuses to act on autonomously
    # (see _is_pr_sweep_flag): a conflicted open PR (#78) and a green
    # solo-overlay PR with no recorded independent cold-review (#68). Both
    # need an operator decision, so they surface in action_needed rather than
    # being dropped with the rest of the diagnostic pr_sweep.* family.
    "pr_sweep.flag_conflict": "action_needed",
    "pr_sweep.flag_no_review": "action_needed",
}

# Diagnostic signal kinds that intentionally do NOT render to the statusline.
# ``outbound.audit_skipped`` is emitted once per unverifiable claim per tick —
# without this drop, N unverifiable claims fill the in_flight zone with N
# identical rows ("No verifier for <kind> overlay=<overlay>") and crowd out
# real signal (#1372). The signal is still emitted so internal counts work;
# only the statusline rendering is suppressed.
_STATUSLINE_DROP_KINDS: frozenset[str] = frozenset({"outbound.audit_skipped"})

# Signal-kind *prefixes* of pure scanner bookkeeping, kept off the statusline.
_STATUSLINE_DROP_PREFIXES: tuple[str, ...] = (
    "self_update.",
    "pull_main_clone.",
    "pr_sweep.",
    "outbound.",
    "review_nag.",
    "review_request_merge_react.",
    "architectural_review.",
    "dogfood_smoke.",
    "scanning_news.",
    # #2190 the idle reaper + queue drainer route to mechanical handlers; their
    # bookkeeping signals must not flood the statusline (a slow drain emits a
    # backoff signal per due item per tick).
    "local_stack.",
)


_SELF_UPDATE_CI_SKIP_REASONS: frozenset[str] = frozenset({"ci_red", "ci_pending", "ci_unknown"})

# pr_sweep flag-level kinds the scanner deliberately did NOT act on (a merge
# conflict, or a missing independent cold-review on a solo overlay). They share
# the ``pr_sweep.`` prefix for log grouping but must escape the diagnostic drop
# so the operator sees them — the same exemption shape as the CI-green-gate
# self_update skip above.
_PR_SWEEP_FLAG_KINDS: frozenset[str] = frozenset({"pr_sweep.flag_conflict", "pr_sweep.flag_no_review"})


def _is_pr_sweep_flag(signal: ScanSignal) -> bool:
    """True for a pr_sweep flag-level signal that must reach the statusline."""
    return signal.kind in _PR_SWEEP_FLAG_KINDS


def _is_self_update_ci_skip(signal: ScanSignal) -> bool:
    """True for a ``self_update.skipped`` held by the CI-green fail-closed gate.

    Only these CI-verdict skips (red / pending / unknown) are user-facing; the
    rest of the ``self_update.*`` family stays diagnostic-only noise. A true
    result exempts the signal from :func:`_is_statusline_dropped` so the
    catch-all renders it in ``action_needed`` — a clone wedged behind a red
    default branch is visible, not silently stale.
    """
    return signal.kind == "self_update.skipped" and (
        str(signal.payload.get("reason", "")) in _SELF_UPDATE_CI_SKIP_REASONS
    )


def _is_statusline_dropped(signal: ScanSignal) -> bool:
    """True when *signal* is diagnostic-only and must not reach the statusline."""
    if _is_self_update_ci_skip(signal) or _is_pr_sweep_flag(signal):
        return False
    if signal.kind == "review_request_merge_react.missing_scope":
        return False
    return signal.kind in _STATUSLINE_DROP_KINDS or signal.kind.startswith(_STATUSLINE_DROP_PREFIXES)


def _first_pr_url(*texts: str) -> str:
    """Return the first PR/MR URL found across *texts*, or ``""``."""
    for text in texts:
        found = find_pr_urls(text)
        if found:
            return found[0]
    return ""


def _slack_pr_url(signal: ScanSignal) -> str:
    """Extract a PR URL from a slack.mention/dm signal if its text contains one."""
    event = signal.payload.get("event")
    if not isinstance(event, dict):
        return ""
    text = event.get("text")
    if not isinstance(text, str):
        return ""
    return _first_pr_url(text)


def _task_pr_url(signal: ScanSignal) -> str:
    """Extract a PR URL from an ``incoming_event.task_needed`` signal.

    The webhook path (``/hooks/slack/`` → IncomingEvent → classifier →
    router → scanner) puts the inbound body in ``payload['detail']`` and
    echoes it into ``summary``. A Slack message like "can you review
    https://…/merge_requests/42" classifies as ``TASK`` (the imperative
    "review" keyword) and would otherwise drop to a passive statusline
    note — the referenced PR never gets an independent review (#219).
    """
    detail = signal.payload.get("detail")
    detail_text = detail if isinstance(detail, str) else ""
    return _first_pr_url(detail_text, signal.summary)


# Reviewer signals dispatch to the agent AND mirror into the statusline so
# the user sees the pending review before the agent acts.
_DUAL_DISPATCH: frozenset[str] = frozenset(
    {
        "reviewer_pr.new_sha",
        "reviewer_pr.unreviewed",
        "reviewer_pr.approval_dismissed",
        # #1047: the reviewer agent runs AND we mirror into the statusline so
        # the user sees the pending review-intent on a colleague's MR.
        "slack.review_intent",
        # #1130: the orchestrator runs AND we mirror the RED CARD into the
        # statusline so the user sees the pending corrective-action workflow.
        "red_card.signal",
        # #1554: the orchestrator runs (maker-side kickoff) AND we mirror the
        # claimed issue into the statusline so the user sees the in-flight
        # auto-implement work.
        "issue_implementer.claimed",
        # #1295 cap D: the t3:debug agent runs AND we mirror the failed
        # PR into the statusline so the user sees the red MR even when
        # the ledger idempotency gate suppresses the agent dispatch on
        # a re-tick of the same head_sha.
        "my_pr.failed",
    },
)

# Signal kind → (DispatchAction kind, zone) when the side-effect is a
# fire-and-forget mechanical handler or webhook rather than an agent.
_MECHANICAL_BY_KIND: dict[str, tuple[ActionKind, str]] = {
    "ticket.completion_detected": ("mechanical", "ticket_completion"),
    "ticket.reopen_needed": ("mechanical", "ticket_reopen"),
    # #998/#1074: a reviewer-role ticket's PENDING/CLAIMED reviewing task
    # can be orphaned when the underlying PR is merged/closed externally
    # before the slot processes it. The scanner emits this signal ONLY
    # after ``get_pr_open_state`` confirmed the PR is genuinely MERGED or
    # CLOSED — never on mere absence from the reviewer-assignment scan
    # (#1074: a Slack-review-request MR with no forge reviewer assignment
    # is permanently absent yet fully OPEN). The mechanical handler then
    # completes the task so ``pending-spawn`` stops surfacing it.
    "reviewer_pr.task_orphaned": ("mechanical", "reviewer_task_orphaned"),
    # #1321: ``list_review_requested_prs`` can surface an MR the user
    # authored (under any of their configured identities). Own MRs must
    # never dispatch ``t3:reviewer`` — they route to coder/debugger + a
    # colleague review-request. The scanner emits this signal when a
    # reviewing task already exists for a self-authored MR so the
    # mechanical handler completes it and the queue self-heals on the next
    # tick (the orphan sweep only reaps MERGED/CLOSED PRs, not open
    # self-authored ones).
    "reviewer_pr.task_self_authored": ("mechanical", "reviewer_task_self_authored"),
    # #1113 Defect 2: ``SlackDmInboundScanner`` emits ``slack.user_reply`` per
    # drained user reply. The real consumer is the reactive Slack-answer loop
    # (``teatree.loop.slack_answer`` — drains the ``PendingChatInjection`` rows
    # this scanner records, see ``slack_dm_inbound`` docstring). Without an
    # explicit mechanical route, the signal fell through to the statusline
    # fallback and leaked raw ``ts``/``text`` verbatim into ``action_needed``.
    "slack.user_reply": ("mechanical", "slack_user_reply"),
    "notion.unrouted": ("webhook", "n8n"),
    # #1295 cap B: Slack @-mention pickup → mechanically assign the user
    # as reviewer on the MR. Once GitLab acknowledges the assignment, the
    # existing ReviewerPrsScanner emits ``reviewer_pr.unreviewed`` which
    # already routes to ``t3:reviewer``; no separate agent wire-up here.
    "review_request_in_slack": ("mechanical", "assign_gitlab_reviewer"),
    # #1295 cap E: failed-E2E Slack-post sweep emits this per failing
    # spec → routes to ``t3:e2e`` for the actual fix attempt.
    "e2e.failure_detected": ("agent", "t3:e2e"),
    # #1295 cap H: ac-reviewing-codebase auto-fix sweep emits this per
    # new finding → routes to ``t3:coder`` for the drift fix.
    "skill_drift_detected": ("agent", "t3:coder"),
    # #128 resource-pressure CRITICAL → mechanical freeing pass (allow-list
    # cache purge / idle-container stop; flag-gated worktree GC + SIGTERM).
    "resource.cleanup_needed": ("mechanical", "free_resources"),
    # #129 TODO-sweep — a task whose artifact is terminal → the mechanical
    # handler RE-checks then completes it (never bulk, never on a stale read).
    "todo.completion_detected": ("mechanical", "todo_completion"),
    # #2122 issue-disposition triage — a high-confidence DEAD issue
    # (already-shipped / exact-duplicate / obsolete) → the mechanical handler
    # closes it idempotently with an audit-trail comment. Never an agent: the
    # scanner can CLOSE noise but is physically unable to enqueue work.
    "issue_disposition.close_candidate": ("mechanical", "close_dead_issue"),
    # #2190 idle-stack reaper → mechanical stop_services (reversible demotion);
    # #44 acquisition-queue drainer → mechanical start_services / backoff. Both
    # are mechanical-only (re-verify live state, never an agent).
    "local_stack.reap_idle": ("mechanical", "reap_idle_stack"),
    "local_stack.queue_acquire": ("mechanical", "drain_stack_queue_item"),
}


def _dispatch_answering(signal: ScanSignal) -> list[DispatchAction]:
    """Route an ``answering``-phase task to the ``t3:answerer`` skill (#670).

    Mirrors the reviewer dual-dispatch: the inbound question becomes a
    ``t3:answerer`` agent invocation plus a statusline mirror so the user
    sees the pending answer before the agent acts. The autonomy level
    (``require_human_approval_to_answer``) is resolved here through the
    standard active-overlay → global → default chain (mirrors
    ``require_human_approval_to_merge``) and stamped into the agent
    payload as an advisory convenience mirror; the answerer skill
    re-resolves the setting at task start (see ``skills/answerer/SKILL.md``
    § Autonomy Gate), so the stamp is a hint, not the source of truth.
    ``coding``-phase task_needed signals are left to the statusline
    fallback — auto ticket creation from inbound chat is a separate
    decision pass (see ``IncomingEventsScanner``).
    """
    # NOTE(#963): a bot→user Slack notification channel (`teatree.notify.notify_user`,
    # setting `notify_user_via_bot`) is slated so agent answers / questions / important
    # info also reach the user's configured Slack via the bot. See souliane/teatree#963.
    require_approval = get_effective_settings().require_human_approval_to_answer
    payload: ActionPayload = {**signal.payload, "require_human_approval_to_answer": require_approval}
    return [
        DispatchAction(kind="agent", zone="t3:answerer", detail=signal.summary, payload=payload),
        DispatchAction(
            kind="statusline",
            zone=_STATUSLINE_ZONE_BY_KIND.get(signal.kind, "action_needed"),
            detail=signal.summary,
            payload=signal.payload,
        ),
    ]


def _dispatch_pending_task(signal: ScanSignal) -> list[DispatchAction] | None:
    """Route a ``pending_task`` signal to its PHASE's own agent (per-phase dispatch).

    The ``PendingTasksScanner`` emits one ``pending_task`` per pending row,
    carrying the row's ``phase`` and ``ticket_role``. The agent is resolved
    through the single canonical ``(role, phase) → agent`` authority
    (``subagent_for_phase``): coding → t3:coder, testing → t3:tester,
    reviewing → t3:reviewer, shipping → t3:shipper. No author phase falls
    through to a single chaining orchestrator — that is the shadowing this
    restores. A pair with no registered agent (free-form phase, or a missing
    role) returns ``None`` so it falls through to the statusline fallback for
    operator triage rather than being misrouted.
    """
    role = str(signal.payload.get("ticket_role", ""))
    phase = str(signal.payload.get("phase", ""))
    agent = subagent_for_phase(role, phase)
    if not agent:
        return None
    return [DispatchAction(kind="agent", zone=agent, detail=signal.summary, payload=signal.payload)]


def _conditional_dispatch(signal: ScanSignal) -> list[DispatchAction] | None:
    """Payload-conditional special cases that precede the generic lookups.

    Each handler returns its action list, or ``None`` to fall through to the
    generic ``_AGENT_BY_KIND`` / ``_MECHANICAL_BY_KIND`` / statusline chain
    in ``_dispatch_one``. A per-kind dispatch table keeps this flat.
    """
    handler = _CONDITIONAL_HANDLERS.get(signal.kind)
    return handler(signal) if handler is not None else None


def _review_target_is_dead(pr_url: str) -> bool:
    """Whether the MR/PR at *pr_url* is provably MERGED or CLOSED (#2081).

    GitLab is the source of truth: a review note can never land on a merged or
    closed MR, so the loop must not dispatch a reviewer for one. Resolves the
    per-URL code host with the active overlay's credentials and reads the live
    state via :meth:`CodeHostBackend.get_pr_open_state`.

    Fail-OPEN doctrine (mirrors ``get_pr_open_state``'s own contract): only a
    *definite* MERGED/CLOSED suppresses. UNKNOWN (any auth error, network
    failure, unparsable URL), an unresolvable host, or any exception returns
    ``False`` so a transient API hiccup never silently drops a legitimate
    review.
    """
    if not pr_url:
        return False
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    try:
        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
        from teatree.core.overlay_loader import get_overlay_for_url  # noqa: PLC0415

        host = get_code_host_for_url(get_overlay_for_url(pr_url), pr_url)
        if host is None:
            return False
        state = host.get_pr_open_state(pr_url=pr_url)
    except Exception:
        logger.exception("Live-state check failed for %s — failing open (still dispatch)", pr_url)
        return False
    return state in {PrOpenState.MERGED, PrOpenState.CLOSED}


def _gate_review_intent(signal: ScanSignal) -> list[DispatchAction] | None:
    """Gate a ``slack.review_intent`` dispatch on review-loop-enabled + live MR state.

    A review-intent dispatch is a claim on a colleague's review. Returning
    ``[]`` suppresses the reviewer dispatch for a signal reaching dispatch from
    any source (not only the scanner that already filters):

    * #79: the review loop is stopped — queue none of them;
    * #2081: the target MR is already MERGED/CLOSED — a note can never land,
        so skip it (GitLab is the source of truth). Fails open on UNKNOWN.

    ``None`` lets the enabled, still-open case fall through to the generic
    ``_AGENT_BY_KIND`` route.
    """
    from teatree.loop.review_claim import review_loop_enabled  # noqa: PLC0415

    if not review_loop_enabled():
        return []
    pr_url = str(signal.payload.get("mr_url") or signal.payload.get("url") or "")
    if _review_target_is_dead(pr_url):
        return []
    return None


def _dispatch_flag_no_review(signal: ScanSignal) -> list[DispatchAction] | None:
    """Route ``pr_sweep.flag_no_review`` by whether the review was auto-dispatched (#68).

    The reviewing task is created by the scanner itself (via the
    :class:`ReviewDispatcher` port) — this only decides the statusline zone:
    ``in_flight`` when a cold review was auto-armed (the loop is handling it),
    ``action_needed`` when it was not (operator triage, the pre-#68 behaviour).
    Returns ``None`` to fall through to the generic statusline route when the
    payload does not flag a dispatch.
    """
    if signal.payload.get("review_dispatched") is not True:
        return None
    return [DispatchAction(kind="statusline", zone="in_flight", detail=signal.summary, payload=signal.payload)]


def _dispatch_slack_message(signal: ScanSignal) -> list[DispatchAction] | None:
    pr_url = _slack_pr_url(signal)
    return _review_request_dispatch(signal, pr_url) if pr_url else None


def _dispatch_assigned_issue(signal: ScanSignal) -> list[DispatchAction] | None:
    if signal.payload.get("auto_start") is not True:
        return None
    return [DispatchAction(kind="agent", zone="t3:orchestrator", detail=signal.summary, payload=signal.payload)]


def _dispatch_incoming_task(signal: ScanSignal) -> list[DispatchAction] | None:
    """Route an ``incoming_event.task_needed`` signal (#219, #670).

    A carried PR/MR URL means a review request regardless of the
    classifier's phase, so it precedes the ``answering`` fallback. An
    ``answering`` phase with no URL routes to the answerer; everything else
    falls through (``None``) to the statusline.
    """
    pr_url = _task_pr_url(signal)
    if pr_url:
        return _review_request_dispatch(signal, pr_url)
    if normalize_phase(str(signal.payload.get("phase", ""))) == "answering":
        return _dispatch_answering(signal)
    return None


def _codex_review_dispatch(signal: ScanSignal) -> list[DispatchAction]:
    """Route ``codex_review.dispatch`` to the variant agent named in the payload (#1254).

    The agent zone is the slash-command name (``codex:review`` or
    ``codex:adversarial-review``) so the runtime invokes the same agent
    the user would have invoked manually. Falls back to ``codex:review``
    when the payload is missing the variant — the standard review is
    the safe default.
    """
    variant = str(signal.payload.get("variant") or "codex:review")
    return [
        DispatchAction(
            kind="agent",
            zone=variant,
            detail=signal.summary,
            payload=signal.payload,
        ),
    ]


def _review_request_dispatch(signal: ScanSignal, pr_url: str) -> list[DispatchAction]:
    """Dual-dispatch a Slack review request to the reviewer agent.

    Shared by the polling path (``slack.mention``/``slack.dm``) and the
    webhook path (``incoming_event.task_needed`` carrying a PR URL, #219):
    an independent ``t3:reviewer`` invocation plus a statusline mirror so
    the user sees the pending review before the agent acts. A review
    request is a review request regardless of the classifier's phase —
    this branch precedes the ``answering`` fallback so "can you review
    MR X" routes to a review, not the answerer.

    #79: a reviewer dispatch is a claim on a colleague's review; when the
    review loop is stopped the loop must queue none of them. The single
    chokepoint every mention/DM/task review-request flows through, so the
    stopped-loop gate lives here rather than scattered across callers.

    #2081: the same chokepoint skips a review whose target MR is already
    MERGED/CLOSED (GitLab is the source of truth — a note can never land on
    one). Fails open on UNKNOWN so a transient API hiccup never drops a
    legitimate review.
    """
    from teatree.loop.review_claim import review_loop_enabled  # noqa: PLC0415

    if not review_loop_enabled():
        return []
    if _review_target_is_dead(pr_url):
        return []
    return [
        DispatchAction(
            kind="agent",
            zone="t3:reviewer",
            detail=f"Review request: {pr_url}",
            payload={"url": pr_url, **signal.payload},
        ),
        DispatchAction(
            kind="statusline",
            zone=_STATUSLINE_ZONE_BY_KIND.get(signal.kind, "action_needed"),
            detail=signal.summary,
            payload=signal.payload,
        ),
    ]


#: Per-kind payload-conditional handlers consulted before the generic lookups.
#: Each returns its action list or ``None`` to fall through (see
#: :func:`_conditional_dispatch`).
_CONDITIONAL_HANDLERS: dict[str, "Callable[[ScanSignal], list[DispatchAction] | None]"] = {
    "pending_task": _dispatch_pending_task,
    "pr_sweep.flag_no_review": _dispatch_flag_no_review,
    "slack.review_intent": _gate_review_intent,
    "slack.mention": _dispatch_slack_message,
    "slack.dm": _dispatch_slack_message,
    "incoming_event.task_needed": _dispatch_incoming_task,
    "assigned_issue.ready": _dispatch_assigned_issue,
    "codex_review.dispatch": _codex_review_dispatch,
}


def _claim_red_mr_fix(signal: ScanSignal) -> bool:
    """Idempotency gate for capability D's ``my_pr.failed`` dispatch.

    Returns True when the ``(pr_url, head_sha)`` pair was not seen on a
    previous tick — the caller proceeds to dispatch the agent. Returns
    False when the same failing SHA already has a recorded attempt —
    the statusline mirror still emits so the user sees the red MR but
    the agent does not re-run. Best-effort: any DB issue defaults to
    True so the fix-attempt path is not silently dropped on a missing
    migration; the statusline always fires.
    """
    from django.db import DatabaseError  # noqa: PLC0415

    pr_url = str(signal.payload.get("pr_url") or signal.payload.get("url") or "")
    head_sha = str(signal.payload.get("head_sha", ""))
    if not pr_url or not head_sha:
        return True
    try:
        from teatree.core.models import RedMrFixAttempt  # noqa: PLC0415

        row = RedMrFixAttempt.claim(
            pr_url=pr_url,
            head_sha=head_sha,
            overlay=str(signal.payload.get("overlay", "")),
            worktree_hint=str(signal.payload.get("worktree_hint", "")),
        )
    except DatabaseError:
        return True
    return row is not None


def _dispatch_one(signal: ScanSignal) -> list[DispatchAction]:
    conditional = _conditional_dispatch(signal)
    if conditional is not None:
        return conditional
    # A registered mechanical handler always wins over a statusline drop: a
    # signal whose prefix is in ``_STATUSLINE_DROP_PREFIXES`` (e.g. the #2190
    # ``local_stack.*`` bookkeeping) must still reach its mechanical executor —
    # the drop only suppresses the *statusline fallback* rendering, not the
    # action. Checked before the drop so the reaper/drainer fire while their
    # signals stay off the statusline.
    mech = _MECHANICAL_BY_KIND.get(signal.kind)
    if mech is not None:
        kind, zone = mech
        return [DispatchAction(kind=kind, zone=zone, detail=signal.summary, payload=signal.payload)]
    if _is_statusline_dropped(signal):
        return []
    agent = _AGENT_BY_KIND.get(signal.kind)
    if agent is not None:
        actions: list[DispatchAction] = []
        # #1295 cap D: gate the agent action on the RedMrFixAttempt
        # idempotency ledger so the same failing head_sha never
        # re-dispatches. The statusline mirror still fires below.
        if signal.kind != "my_pr.failed" or _claim_red_mr_fix(signal):
            actions.append(
                DispatchAction(kind="agent", zone=agent, detail=signal.summary, payload=signal.payload),
            )
        if signal.kind in _DUAL_DISPATCH:
            actions.append(
                DispatchAction(
                    kind="statusline",
                    zone=_STATUSLINE_ZONE_BY_KIND.get(signal.kind, "in_flight"),
                    detail=signal.summary,
                    payload=signal.payload,
                ),
            )
        return actions
    if signal.kind == "ticket.disposition_candidate" and signal.payload.get("reason") == "issue_closed":
        return [
            DispatchAction(kind="mechanical", zone="ticket_disposition", detail=signal.summary, payload=signal.payload),
        ]
    zone = _STATUSLINE_ZONE_BY_KIND.get(signal.kind, "in_flight")
    return [DispatchAction(kind="statusline", zone=zone, detail=signal.summary, payload=signal.payload)]


def dispatch(
    signals: list[ScanSignal],
    *,
    errors: dict[str, str] | None = None,
) -> list[DispatchAction]:
    """Turn a flat list of signals into the actions the runtime should perform.

    Slack mentions/DMs that carry a PR URL also emit a derived
    ``review_channel.request`` agent action — the URL extraction lives here
    rather than in a second scanner so the messaging backend is hit once
    per tick.

    Per-signal exceptions are swallowed so a bad handler never aborts the
    tick.  When *errors* is supplied (a ``TickReport.errors`` dict), each
    swallowed exception is recorded under ``'dispatch:<signal.kind>'`` so it
    surfaces in the action-needed render.
    """
    actions: list[DispatchAction] = []
    for signal in signals:
        try:
            actions.extend(_dispatch_one(signal))
        except Exception as exc:
            logger.exception("Signal %s raised during dispatch", signal.kind)
            if errors is not None:
                errors[f"dispatch:{signal.kind}"] = f"{type(exc).__name__}: {exc}"
    return actions
