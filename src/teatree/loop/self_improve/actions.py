"""Five-rung action ladder for self-improve firings (BLUEPRINT § 5.7).

Each ladder rung is a no-op upgrade over the previous one:

    ``log`` -> ``statusline`` -> ``slack`` -> ``ticket`` -> ``auto_fix``

Phase 1 only mechanically reaches ``statusline`` for every detector and
``slack`` for ``ForgottenMergeDetector``; ``ticket`` is plumbed but
gated by detector-declared ``max_rung``; ``auto_fix`` is whitelisted
(``StaleStatuslineEntryDetector`` only).  The full rung table:

    log         — durable row only; no UI surface
    statusline  — ``ScanSignal`` rendered in the action_needed zone
    slack       — self-DM via the active overlay's MessagingBackend
    ticket      — open a teatree internal-backlog issue (no posting)
    auto_fix    — execute the detector's idempotent self-heal callable
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport, fresh_or_escalated
from teatree.loop.self_improve.persistence import (
    SLACK_RATE_CAP_SECONDS,
    latest_firing,
    recent_slack_firings_within,
    record_firing,
)

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)


_LADDER_ORDER: tuple[str, ...] = (
    ActionRung.LOG,
    ActionRung.STATUSLINE,
    ActionRung.SLACK,
    ActionRung.TICKET,
    ActionRung.AUTO_FIX,
)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The rung that ran, the firing row recorded, and any side-effect summary."""

    rung: str
    firing: SelfImproveFiring
    slack_capped: bool = False
    auto_fix_executed: bool = False


def _ceiling_index(report: DetectorReport) -> int:
    """Resolve ``max_rung`` (declared by the detector) to an index in ``_LADDER_ORDER``."""
    ceiling = report.max_rung
    if ceiling not in _LADDER_ORDER:
        return _LADDER_ORDER.index(ActionRung.STATUSLINE)
    return _LADDER_ORDER.index(ceiling)


def _next_rung_index(report: DetectorReport, firing: SelfImproveFiring | None, ceiling_index: int) -> int:
    """Compute the rung index for this observation.

    An ``auto_fix`` report targets its ceiling (``auto_fix``) on the FIRST
    firing: the self-heal is idempotent and side-effect-free, so it must run
    the moment the smell is observed rather than climb the graduated ladder.
    The climb would never reach ``auto_fix`` for a persistent smell anyway — the
    same stale state hashes to the same ``state_hash`` every tick, so
    ``fresh_or_escalated`` suppresses it at rung 1 and it can never escalate
    (the wired-but-unreachable bug #2625 Part B fixes).

    Every other (non-auto-fix) report keeps the monotonic ladder: first firing
    ⇒ rung 1 (``statusline``) when within the ceiling, else the ceiling;
    subsequent escalation ⇒ one rung up from the last recorded action, bounded
    by the ceiling.
    """
    if report.auto_fix:
        return ceiling_index
    if firing is None:
        return min(_LADDER_ORDER.index(ActionRung.STATUSLINE), ceiling_index)
    current = _LADDER_ORDER.index(firing.last_action) if firing.last_action in _LADDER_ORDER else 0
    return min(current + 1, ceiling_index)


def format_slack_payload(report: DetectorReport) -> dict[str, str]:
    """Build the Slack DM payload for a self-improve firing.

    One single location for the channel and the message shape so the
    post-#963 "switch to the bot channel" flip is a one-variable change.
    """
    channel = report.payload.get("slack_channel", "")
    text = (
        f"[self-improve] {report.detector}: {report.summary}\n"
        f"  severity: {report.severity}\n"
        f"  dedup_key: {report.dedup_key}"
    )
    return {"channel": str(channel), "text": text}


def _slack_capped(now_count: int) -> bool:
    """Has the global Slack rate cap been hit in the trailing window?"""
    return now_count >= 1


def run_action_ladder(
    report: DetectorReport,
    *,
    messaging: "MessagingBackend | None" = None,
    auto_fix_callable: Callable[[DetectorReport], None] | None = None,
) -> ActionResult | None:
    """Advance the ladder by at most one rung for one detector report.

    Returns ``None`` when the firing is suppressed by cool-down (same
    ``state_hash`` as the last firing); otherwise records the new
    ``SelfImproveFiring`` row and executes the side effect for the
    resolved rung.

    The Slack rate cap is enforced before the messaging call: when the
    cap is hit the rung is downgraded to ``statusline`` and the firing
    row records ``slack_capped=True`` via ``last_action="statusline"``.
    """
    existing = latest_firing(report.detector, report.dedup_key)
    if not fresh_or_escalated(report, existing):
        return None

    ceiling_index = _ceiling_index(report)
    rung_index = _next_rung_index(report, existing, ceiling_index)
    rung = _LADDER_ORDER[rung_index]

    slack_capped = False
    auto_fix_executed = False

    if rung == ActionRung.SLACK:
        recent = recent_slack_firings_within(SLACK_RATE_CAP_SECONDS)
        if _slack_capped(recent):
            rung = ActionRung.STATUSLINE
            slack_capped = True
        elif messaging is not None:
            payload = format_slack_payload(report)
            messaging.post_message(channel=payload["channel"], text=payload["text"])
    elif rung == ActionRung.AUTO_FIX:
        if not report.auto_fix:
            # Detector did not opt in to auto-fix — refuse to execute
            # even when the ceiling resolves there.  The structural test
            # enumerates this constraint.
            rung = ActionRung.STATUSLINE
        elif auto_fix_callable is not None:
            auto_fix_callable(report)
            auto_fix_executed = True

    firing = record_firing(report, action=rung)
    return ActionResult(
        rung=rung,
        firing=firing,
        slack_capped=slack_capped,
        auto_fix_executed=auto_fix_executed,
    )
