"""Static routing tables and action data types for the loop dispatcher.

The dispatcher (:mod:`teatree.loop.dispatch`) routes each ``ScanSignal`` to
an action by consulting these maps. Splitting the pure data out of the
dispatcher keeps the routing logic readable: the maps below are *what* each
signal kind routes to; the dispatcher is the *order* the maps are consulted.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

ActionKind = Literal["statusline", "agent", "webhook", "mechanical"]
type ActionPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class DispatchAction:
    """One side-effect produced by the dispatcher for a tick."""

    kind: ActionKind
    zone: str  # statusline zone or agent name (depending on kind)
    detail: str
    payload: ActionPayload = field(default_factory=dict)


AGENT_BY_KIND: dict[str, str] = {
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

STATUSLINE_ZONE_BY_KIND: dict[str, str] = {
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
    # Only the CI-green-gate skips reach here (see is_self_update_ci_skip); a
    # clone wedged behind a red default branch must surface, not stay silent.
    "self_update.skipped": "action_needed",
    # Operator config gap, not per-MR bookkeeping — exempted from the drop below.
    "review_request_merge_react.missing_scope": "action_needed",
    # pr_sweep flag-level signals the scanner refuses to act on autonomously
    # (see is_pr_sweep_flag): a conflicted open PR (#78), a green
    # solo-overlay PR with no recorded independent cold-review (#68), and a PR
    # red only on repo-state checks against a stale base that a rerun can't fix
    # (#2045 — only a merge-update can). All need an operator decision, so they
    # surface in action_needed rather than being dropped with the rest of the
    # diagnostic pr_sweep.* family.
    "pr_sweep.flag_conflict": "action_needed",
    "pr_sweep.flag_no_review": "action_needed",
    "pr_sweep.needs_branch_update": "action_needed",
}

# Diagnostic signal kinds that intentionally do NOT render to the statusline.
# ``outbound.audit_skipped`` is emitted once per unverifiable claim per tick —
# without this drop, N unverifiable claims fill the in_flight zone with N
# identical rows ("No verifier for <kind> overlay=<overlay>") and crowd out
# real signal (#1372). The signal is still emitted so internal counts work;
# only the statusline rendering is suppressed.
STATUSLINE_DROP_KINDS: frozenset[str] = frozenset({"outbound.audit_skipped"})

# Signal-kind *prefixes* of pure scanner bookkeeping, kept off the statusline.
STATUSLINE_DROP_PREFIXES: tuple[str, ...] = (
    "self_update.",
    "pull_main_clone.",
    "pr_sweep.",
    "outbound.",
    "review_nag.",
    "review_request_merge_react.",
    "architectural_review.",
    "dogfood_smoke.",
    "scanning_news.",
    "backlog_sweep.",
    # #2190 the idle reaper + queue drainer route to mechanical handlers; their
    # bookkeeping signals must not flood the statusline (a slow drain emits a
    # backoff signal per due item per tick).
    "local_stack.",
)

SELF_UPDATE_CI_SKIP_REASONS: frozenset[str] = frozenset({"ci_red", "ci_pending", "ci_unknown"})

# pr_sweep flag-level kinds the scanner deliberately did NOT act on: a merge
# conflict, a missing independent cold-review on a solo overlay, or a PR red
# only on repo-state checks against a stale base that needs a merge-update
# (#2045). They share the ``pr_sweep.`` prefix for log grouping but must escape
# the diagnostic drop so the operator sees them — the same exemption shape as
# the CI-green-gate self_update skip above.
PR_SWEEP_FLAG_KINDS: frozenset[str] = frozenset(
    {"pr_sweep.flag_conflict", "pr_sweep.flag_no_review", "pr_sweep.needs_branch_update"}
)

# Reviewer signals dispatch to the agent AND mirror into the statusline so
# the user sees the pending review before the agent acts.
DUAL_DISPATCH: frozenset[str] = frozenset(
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
MECHANICAL_BY_KIND: dict[str, tuple[ActionKind, str]] = {
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
