"""Durable audit-ledger operations for the bot→user notification egress.

The ``BotPing`` delivery-claim/status rows, the ``OutboundClaim`` audit row, and
the ``PendingChatInjection`` answered-stamp that :func:`teatree.core.notify.notify_user_outcome`
orchestrates around a DM. Split out of ``notify.py`` so the egress keeps the
orchestration and this module owns the durable-row mechanics. Every helper is
no-op-safe and never raises into the CLI turn (the egress's never-raise contract).
"""

import logging
import os
import re

from django.db import DatabaseError, IntegrityError, transaction

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify_types import NotifyKind, NotifyOutcome, NotifyReason, blocked
from teatree.core.session_identity import current_session_id

# Logs on the egress channel, not this module's own: the "DID NOT DELIVER" SHOUT
# and the ledger warnings are part of the notify egress's observable contract
# (doctor + assertLogs read them under ``teatree.core.notify``).
logger = logging.getLogger("teatree.core.notify")

# Idempotency-key convention for replies to a Slack-DM question (#1063):
# ``answer-<anything>-<slack_ts>``. ``slack_ts`` is the Slack message
# timestamp (e.g. ``1700000000.0001``) of the question the agent is
# answering. When notify_user sees a key with this shape it auto-stamps
# ``answered_at`` on the matching :class:`PendingChatInjection` row, so
# the Stop hook stops nagging once the reply has been posted.
_ANSWER_KEY_PATTERN = re.compile(r"^answer-.+-(\d+\.\d+)$")


def already_sent_noop(idempotency_key: str) -> NotifyOutcome | None:
    """Fast SENT-idempotency no-op: a sent outcome if already delivered, else ``None``.

    An already-delivered key is a no-op even when the backend is now
    unconfigured (the prior shape checked SENT before resolving the
    backend). A read-only lookup; the atomic ``claim_delivery`` is still the
    authoritative double-DM gate. ``None`` means "not yet SENT — proceed".
    A ``DatabaseError`` fails closed (a named not-sent outcome) — never escapes
    the never-raise contract.
    """
    from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        if BotPing.objects.filter(idempotency_key=idempotency_key, status=BotPing.Status.SENT).exists():
            logger.debug("notify_user idempotent no-op for key=%s", idempotency_key)
            return NotifyOutcome(sent=True, reason=NotifyReason.ALREADY_SENT)
    except DatabaseError as exc:
        logger.warning("notify_user idempotency-ledger read failed for key=%s: %s", idempotency_key, exc)
        return blocked(NotifyReason.LEDGER_UNAVAILABLE, error=str(exc))
    return None


def claim_delivery_slot(
    idempotency_key: str,
    *,
    kind: str,
    text: str,
    audience: str = "",
) -> NotifyOutcome | None:
    """Atomically claim the right to deliver — the double-DM TOCTOU gate.

    Returns ``None`` when this caller won the claim and must proceed to
    deliver; otherwise the early-exit outcome for ``notify_user``: an
    ALREADY_SENT idempotent no-op, or a not-sent outcome when a concurrent tick
    already claimed delivery or a ``DatabaseError`` forces a fail-closed.

    ``BotPing.claim_delivery`` mirrors the ``OnBehalfApproval.consume`` /
    ``LoopLease.acquire`` CAS doctrine: a ``select_for_update`` re-read
    inside one ``transaction.atomic`` so on the production SQLite backend
    the second concurrent tick blocks on the IMMEDIATE write lock, then
    observes the SENDING row and stands down — exactly one tick delivers. A
    prior FAILED/NOOP row is a recoverable retry (#1306) the winner replaces
    with a fresh SENDING claim. A ``DatabaseError`` must not escape the
    never-raise contract.
    """
    from teatree.core.models import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        BotPing,
        DeliveryClaim,
    )

    try:
        claim = BotPing.claim_delivery(idempotency_key, kind=kind, text=text, audience=audience)
    except DatabaseError as exc:
        logger.warning("notify_user delivery-claim access failed for key=%s: %s", idempotency_key, exc)
        return blocked(NotifyReason.LEDGER_UNAVAILABLE, error=str(exc))
    if claim == DeliveryClaim.CLAIMED:
        return None
    if claim == DeliveryClaim.ALREADY_SENT:
        logger.debug("notify_user idempotent no-op for key=%s", idempotency_key)
        return NotifyOutcome(sent=True, reason=NotifyReason.ALREADY_SENT)
    logger.info("notify_user delivery already claimed by a concurrent tick for key=%s", idempotency_key)
    return blocked(NotifyReason.CLAIMED_BY_CONCURRENT_TICK)


def maybe_stamp_answered(*, idempotency_key: str, answering_slack_ts: str) -> None:
    """Auto-stamp ``PendingChatInjection.answered_at`` when this DM is a reply (#1063).

    Two trigger forms, both consulted (an explicit kwarg wins over the
    pattern-match — the kwarg is the canonical, programmatic form). One:
    ``answering_slack_ts="1700000000.0001"`` — the caller explicitly
    passes the Slack ts of the question being answered. Two:
    ``idempotency_key="answer-<anything>-<slack_ts>"`` — the agent used
    the answer-key convention; the ts is extracted from the suffix.

    The stamp keys on ``slack_ts`` alone — symmetric with the unscoped
    Stop-hook gate — so a reply sent from one overlay's session clears a
    question recorded under a *different* overlay (the concurrent multi-
    overlay case). Scoping by the active ``T3_OVERLAY_NAME`` here was the
    original defect: it stamped 0 rows whenever the answering session's
    overlay differed from the recording overlay, leaving the gate nagging.
    """
    ts = answering_slack_ts
    if not ts:
        match = _ANSWER_KEY_PATTERN.match(idempotency_key)
        if match is None:
            return
        ts = match.group(1)
    # Deferred import (mirrors ``maybe_linkify`` in the egress module): the
    # answer-stamp is an opt-in side path; keeping
    # the model import out of module import time avoids
    # perturbing the module-import graph that the on-behalf gate and
    # notify suites rely on.
    from teatree.core.models import PendingChatInjection  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        PendingChatInjection.agent_answered_question(ts)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break notify_user
        logger.debug("notify_user answered_at stamp failed for ts=%s: %s", ts, exc)


def record_outbound_claim(
    *,
    idempotency_key: str,
    target_url: str,
    channel: str,
    posted_ts: str,
) -> None:
    """Record an :class:`OutboundClaim` row for the outbound-audit verifier (#1019).

    Best-effort — never breaks the publish path. The audit scanner reads
    this ledger on the next tick and DMs the user on drift. Inlined here
    (instead of delegating to :func:`teatree.outbound_claim.record_claim`)
    because :mod:`teatree.outbound_claim` lives outside ``teatree.core``
    and adding ``teatree.core → teatree.outbound_claim`` would cycle
    through ``teatree.outbound_claim → teatree.core``.

    Unlike the sibling :func:`teatree.outbound_claim.record_claim` (which
    returns ``OutboundClaim | None``), this helper intentionally returns
    ``None``: there is no consumer of the row here (the publish path
    ignores it), so adding a return value would be dead code — a future
    sibling-sync pass should not "fix" this asymmetry.
    """
    from teatree.core.models import OutboundClaim  # noqa: PLC0415 — deferred: ORM import needs the app registry

    session_id = current_session_id()
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "") or ""
    try:
        with transaction.atomic():
            OutboundClaim.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    "kind": OutboundClaim.Kind.SLACK_DM.value,
                    "target_url": target_url,
                    "agent_session_id": session_id,
                    "extra": {
                        "channel": channel,
                        "ts": posted_ts,
                        "overlay": overlay_name,
                    },
                },
            )
    except IntegrityError:
        logger.debug("notify_user outbound-claim race on key=%s", idempotency_key)
    except DatabaseError as exc:
        logger.warning("notify_user outbound-claim DB failure for key=%s: %s", idempotency_key, exc)
    except Exception as exc:  # noqa: BLE001 — claim ledger is best-effort
        logger.debug("notify_user outbound-claim record failed for key=%s: %s", idempotency_key, exc)


def record_noop(
    *,
    idempotency_key: str,
    kind: NotifyKind,
    text: str,
    audience: NotifyAudience,
    reason: NotifyReason,
) -> None:
    """Record the un-sent notification and SHOUT about it.

    ERROR level, not debug: nobody is watching the CLI turn this fires in — that is
    the entire premise of the egress — so the log line is one of the few traces a
    later investigation has. The durable half is the ``BotPing`` row, which
    ``teatree.cli.doctor.checks_slack_roundtrip`` reads back as evidence.
    """
    from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: ORM import needs the app registry

    logger.error(
        "notify_user DID NOT DELIVER — owner NOT notified. key=%s audience=%s reason=%s: %s",
        idempotency_key,
        audience.value,
        reason.value,
        reason.detail,
    )
    try:
        with transaction.atomic():
            BotPing.objects.create(
                idempotency_key=idempotency_key,
                kind=kind.value,
                status=BotPing.Status.NOOP,
                text=text,
                audience=audience.value,
                error_message=reason.detail,
            )
    except IntegrityError:
        logger.debug("notify_user noop race on key=%s", idempotency_key)
    except DatabaseError as exc:
        logger.warning("notify_user noop audit write failed for key=%s: %s", idempotency_key, exc)


def finalize_sent(*, idempotency_key: str, channel: str, posted_ts: str, permalink: str) -> None:
    """Stamp the claimed SENDING row terminal-SENT — never raises (the DM has landed)."""
    from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        BotPing.finalize_sent(
            idempotency_key,
            channel_ref=channel,
            posted_ts=posted_ts,
            permalink=permalink,
        )
    except DatabaseError as exc:
        # The DM has already landed; a locked/failed audit write must never
        # escape and break the caller's FSM transition (the never-raise
        # contract). Mirror ``record_outbound_claim``.
        logger.warning("notify_user sent-finalize write failed for key=%s: %s", idempotency_key, exc)


def finalize_failed(*, idempotency_key: str, error: str) -> None:
    """Stamp the claimed SENDING row terminal-FAILED so a later retry recovers it."""
    from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        BotPing.finalize_failed(idempotency_key, error=error)
    except DatabaseError as exc:
        logger.warning("notify_user failed-finalize write failed for key=%s: %s", idempotency_key, exc)
