"""Pure decision logic for the loop dispatcher.

Every function here is a pure transformation of a ``ScanSignal`` (and the
static routing tables) into an action list — no network, no DB, no settings
resolution. Those *seams* live in :mod:`teatree.loop.dispatch_gates`, while
:mod:`teatree.loop.dispatch` owns the orchestrating ``dispatch`` /
``_dispatch_one`` entrypoints. Keeping the pure predicates and per-kind
routers here makes them trivially testable and keeps the dispatcher module
focused on the seams and the consult order.
"""

from teatree.core.modelkit.phases import subagent_for_phase
from teatree.loop.dispatch_tables import (
    PR_SWEEP_FLAG_KINDS,
    SELF_UPDATE_CI_SKIP_REASONS,
    STATUSLINE_DROP_KINDS,
    STATUSLINE_DROP_PREFIXES,
    DispatchAction,
)
from teatree.loop.scanners.base import ScanSignal
from teatree.url_classify import find_pr_urls


def is_pr_sweep_flag(signal: ScanSignal) -> bool:
    """True for a pr_sweep flag-level signal that must reach the statusline."""
    return signal.kind in PR_SWEEP_FLAG_KINDS


def is_self_update_ci_skip(signal: ScanSignal) -> bool:
    """True for a ``self_update.skipped`` held by the CI-green fail-closed gate.

    Only these CI-verdict skips (red / pending / unknown) are user-facing; the
    rest of the ``self_update.*`` family stays diagnostic-only noise. A true
    result exempts the signal from :func:`is_statusline_dropped` so the
    catch-all renders it in ``action_needed`` — a clone wedged behind a red
    default branch is visible, not silently stale.
    """
    return signal.kind == "self_update.skipped" and (
        str(signal.payload.get("reason", "")) in SELF_UPDATE_CI_SKIP_REASONS
    )


def is_statusline_dropped(signal: ScanSignal) -> bool:
    """True when *signal* is diagnostic-only and must not reach the statusline."""
    if is_self_update_ci_skip(signal) or is_pr_sweep_flag(signal):
        return False
    if signal.kind == "review_request_merge_react.missing_scope":
        return False
    # T4-PR-3: the outer loop's keep/revert DECISION is operator-facing and must
    # escape the ``outer_loop.`` drop prefix (which suppresses the per-tick
    # ``outer_loop.refused`` bookkeeping) — the same exemption shape as the
    # CI-green self_update skip and the pr_sweep flags above.
    if signal.kind == "outer_loop.decision":
        return False
    return signal.kind in STATUSLINE_DROP_KINDS or signal.kind.startswith(STATUSLINE_DROP_PREFIXES)


def first_pr_url(*texts: str) -> str:
    """Return the first PR/MR URL found across *texts*, or ``""``."""
    for text in texts:
        found = find_pr_urls(text)
        if found:
            return found[0]
    return ""


def slack_pr_url(signal: ScanSignal) -> str:
    """Extract a PR URL from a slack.mention/dm signal if its text contains one."""
    event = signal.payload.get("event")
    if not isinstance(event, dict):
        return ""
    text = event.get("text")
    if not isinstance(text, str):
        return ""
    return first_pr_url(text)


def task_pr_url(signal: ScanSignal) -> str:
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
    return first_pr_url(detail_text, signal.summary)


def dispatch_pending_task(signal: ScanSignal) -> list[DispatchAction] | None:
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


def dispatch_flag_no_review(signal: ScanSignal) -> list[DispatchAction] | None:
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


def dispatch_assigned_issue(signal: ScanSignal) -> list[DispatchAction] | None:
    if signal.payload.get("auto_start") is not True:
        return None
    return [DispatchAction(kind="agent", zone="t3:orchestrator", detail=signal.summary, payload=signal.payload)]


def codex_review_dispatch(signal: ScanSignal) -> list[DispatchAction]:
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
