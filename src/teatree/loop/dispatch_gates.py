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
from teatree.loop.scanners.pr_payload import head_sha as _extract_head_sha

logger = logging.getLogger(__name__)

#: The display marker an untyped/empty spawn degrades to in :func:`spawn_display_name`.
#: A real phase agent is always ``t3:<type>``, so this never names a live phase agent.
GENERAL_PURPOSE_SUBAGENT = "general-purpose"

#: Head-SHA placeholder for a red PR whose real sha the scanner never carried.
#: The claim is keyed on ``(pr_url, sentinel)`` so at most ONE fix dispatches for
#: that PR until a real sha appears — replacing the old blank-sha fail-OPEN
#: (``return True`` forever) that let the same red PR re-dispatch every tick (#7).
_BLANK_SHA_SENTINEL = "sha-unavailable"


def spawn_display_name(subagent: str, task_id: int) -> str:
    """The ``t3-<type>-<id>`` display name a dispatched sub-agent must carry (PR-12).

    Every spawn is named after its phase agent type and the task it serves, so a
    spawned agent is attributable at a glance (``t3-coder-42``) and never an
    anonymous ``general-purpose`` one. The ``t3:`` namespace is folded to the
    ``t3-`` display prefix; an untyped/empty *subagent* degrades to the explicit
    ``general-purpose`` marker rather than an empty name.
    """
    agent_type = subagent.removeprefix("t3:").strip() or GENERAL_PURPOSE_SUBAGENT
    return f"t3-{agent_type}-{task_id}"


def review_target_is_dead(pr_url: str) -> bool:
    """Whether the loop must skip dispatching a reviewer for *pr_url* (#2081).

    A review note can never land on a merged or closed MR, so a provably
    MERGED/CLOSED target is dead. Fail-OPEN: anything indefinite still
    dispatches — see :func:`teatree.backends.loader.pr_is_merged_or_closed`.
    """
    from teatree.backends.loader import pr_is_merged_or_closed  # noqa: PLC0415 — deferred: loaded at tick time

    return pr_is_merged_or_closed(pr_url)


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
    # NOTE(#963): a bot→user Slack notification channel (`teatree.core.notify.notify_user`,
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


def claim_red_mr_fix(payload: ActionPayload) -> bool:
    """Idempotency claim for capability D's ``my_pr.failed`` fix dispatch.

    Called at PERSIST time (``persistence._handle_debug``, #1 blocker fix), not
    at dispatch time: the claim rides the same ``transaction.atomic`` block that
    creates the debugging Task, so a dropped/failed persist rolls the claim back
    and the next tick retries — the marker can no longer be burned before the
    action lands.

    Returns True when the ``(pr_url, head_sha)`` pair was not seen on a previous
    tick — the caller proceeds to create the Task. Returns False when the same
    failing SHA already has a recorded attempt — the statusline mirror still
    emitted at dispatch so the user sees the red MR, but no new Task is created.

    SIG-2 (#7) hardens WHAT is claimed: the real head sha is recovered from
    ``payload['raw']`` via the shared dual-forge helper when the top-level
    ``head_sha`` is blank; a still-blank sha claims the :data:`_BLANK_SHA_SENTINEL`
    keyed on ``pr_url`` (at most one dispatch per PR) and logs the instrumentation
    gap — replacing the old fail-OPEN that re-dispatched forever. A ``DatabaseError``
    still fails open (a missing migration must not silently drop the fix path) but
    now logs at WARNING with the ``pr_url`` so the fail-open is visible and bounded.
    """
    from django.db import DatabaseError  # noqa: PLC0415 — deferred: Django import at call time

    pr_url = str(payload.get("pr_url") or payload.get("url") or "")
    if not pr_url:
        return True
    sha = str(payload.get("head_sha") or "") or _sha_from_raw(payload)
    if not sha:
        logger.warning(
            "claim_red_mr_fix: no head sha for red PR %s — claiming sentinel (one dispatch, gap surfaced)",
            pr_url,
        )
        sha = _BLANK_SHA_SENTINEL
    try:
        from teatree.core.models import RedMrFixAttempt  # noqa: PLC0415 — deferred: ORM import needs the app registry

        row = RedMrFixAttempt.claim(
            pr_url=pr_url,
            head_sha=sha,
            overlay=str(payload.get("overlay", "")),
            worktree_hint=str(payload.get("worktree_hint", "")),
        )
    except DatabaseError:
        logger.warning("claim_red_mr_fix: DB error claiming %s — failing open (bounded)", pr_url)
        return True
    return row is not None


def _sha_from_raw(payload: ActionPayload) -> str:
    """Recover the head sha from the raw forge PR dict on the payload, or ``""``."""
    raw = payload.get("raw")
    return _extract_head_sha(raw) if isinstance(raw, dict) else ""
