"""Network/DB seams for the loop dispatcher.

Unlike :mod:`teatree.loop.dispatch_reducer` (pure routing), every function
here reaches out to an *external* source of truth before deciding: the live
code-host state of a PR/MR, the ``RedMrFixAttempt`` idempotency ledger, or
the resolved effective settings. They are isolated from the pure reducer so a
test can pin or stub the seam without touching the routing tables, and so the
dispatcher module stays focused on the consult order.
"""

import logging

from teatree.config import get_effective_settings
from teatree.core.modelkit.phases import normalize_phase
from teatree.loop.dispatch_reducer import slack_pr_url, task_pr_url
from teatree.loop.dispatch_tables import STATUSLINE_ZONE_BY_KIND, ActionPayload, DispatchAction
from teatree.loop.review_claim_signals import review_loop_enabled
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)


def review_target_is_dead(pr_url: str) -> bool:
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


def dispatch_answering(signal: ScanSignal) -> list[DispatchAction]:
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
            zone=STATUSLINE_ZONE_BY_KIND.get(signal.kind, "action_needed"),
            detail=signal.summary,
            payload=signal.payload,
        ),
    ]


def gate_review_intent(signal: ScanSignal) -> list[DispatchAction] | None:
    """Gate a ``slack.review_intent`` dispatch on review-loop-enabled + live MR state.

    A review-intent dispatch is a claim on a colleague's review. Returning
    ``[]`` suppresses the reviewer dispatch for a signal reaching dispatch from
    any source (not only the scanner that already filters):

    * #79: the review loop is stopped — queue none of them;
    * #2081: the target MR is already MERGED/CLOSED — a note can never land,
        so skip it (GitLab is the source of truth). Fails open on UNKNOWN.

    ``None`` lets the enabled, still-open case fall through to the generic
    ``AGENT_BY_KIND`` route.
    """
    if not review_loop_enabled():
        return []
    pr_url = str(signal.payload.get("mr_url") or signal.payload.get("url") or "")
    if review_target_is_dead(pr_url):
        return []
    return None


def dispatch_slack_message(signal: ScanSignal) -> list[DispatchAction] | None:
    pr_url = slack_pr_url(signal)
    return review_request_dispatch(signal, pr_url) if pr_url else None


def dispatch_incoming_task(signal: ScanSignal) -> list[DispatchAction] | None:
    """Route an ``incoming_event.task_needed`` signal (#219, #670).

    A carried PR/MR URL means a review request regardless of the
    classifier's phase, so it precedes the ``answering`` fallback. An
    ``answering`` phase with no URL routes to the answerer; everything else
    falls through (``None``) to the statusline.
    """
    pr_url = task_pr_url(signal)
    if pr_url:
        return review_request_dispatch(signal, pr_url)
    if normalize_phase(str(signal.payload.get("phase", ""))) == "answering":
        return dispatch_answering(signal)
    return None


def review_request_dispatch(signal: ScanSignal, pr_url: str) -> list[DispatchAction]:
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
    if not review_loop_enabled():
        return []
    if review_target_is_dead(pr_url):
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
            zone=STATUSLINE_ZONE_BY_KIND.get(signal.kind, "action_needed"),
            detail=signal.summary,
            payload=signal.payload,
        ),
    ]


def claim_red_mr_fix(signal: ScanSignal) -> bool:
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
