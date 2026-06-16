"""Verified-delivery notify wrapper with automatic fallback transport (#1181).

The contract this module enforces is *delivery*, not "called send". The
canonical bot→user DM egress is :func:`teatree.notify.notify_user`, but it
returned ``did not deliver`` (rc=1 at the CLI edge) silently this session —
root cause under #1173. When that primary path fails, the agent has been
manually falling back to a direct Slack send. This wrapper makes that
fallback structural and verified instead of relying on agent vigilance.

The flow: try the canonical :func:`notify_user` path first; on a
non-delivery (its ``False`` / CLI rc!=0 contract) fall back to a **direct**
messaging-backend send (same channel + body + idempotency-key) — a
transport independent of whatever broke the primary path. The fallback send
is then **round-trip verified** by re-reading the posted message
(``fetch_message``): a direct send whose round-trip read finds nothing is a
HARD FAILURE, never a phantom success. Finally the transport that actually
delivered is recorded on the :class:`BotPing` row, and the original primary
failure is kept in ``error_message`` so #1173 stays diagnosable.

This is the resilience "fallback-transport" invariant: a primary failure
triggers a second, independent, verified attempt — never a silent drop.
"""

import enum
import logging
from dataclasses import dataclass

from django.db import DatabaseError, IntegrityError, transaction

from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import BotPing
from teatree.core.notify import NotifyKind, format_notification, maybe_linkify, resolve_user_id
from teatree.notify import notify_user

logger = logging.getLogger(__name__)


class NotifyTransport(enum.StrEnum):
    """Which transport delivered the DM (mirrors :class:`BotPing.Transport`)."""

    NONE = ""
    PRIMARY = "primary"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """Outcome of a :func:`notify_with_fallback` call.

    ``delivered`` is the only contract callers must honour — it is ``True``
    only on a confirmed primary delivery or a round-trip-verified fallback.
    ``transport`` records which path landed it (``NONE`` when nothing did).
    """

    delivered: bool
    transport: NotifyTransport


@dataclass(frozen=True, slots=True)
class _DeliveredSend:
    """A confirmed, round-trip-verified direct send awaiting its audit row."""

    channel: str
    posted_ts: str
    permalink: str


def notify_with_fallback(
    text: str,
    *,
    kind: NotifyKind | str,
    idempotency_key: str,
    user_id: str | None = None,
    linkify: bool = True,
) -> NotifyResult:
    """Deliver a bot→user DM, falling back to a direct verified send.

    Returns a :class:`NotifyResult` recording delivery and the transport
    that landed it. Never raises into the calling turn — a delivery failure
    is reported via ``delivered=False`` and a FAILED :class:`BotPing` row.
    """
    kind_value = NotifyKind(kind) if not isinstance(kind, NotifyKind) else kind

    primary_delivered = notify_user(
        text,
        kind=kind_value,
        idempotency_key=idempotency_key,
        user_id=user_id,
        linkify=linkify,
    )
    if primary_delivered:
        _stamp_transport(idempotency_key, NotifyTransport.PRIMARY)
        return NotifyResult(delivered=True, transport=NotifyTransport.PRIMARY)

    if not _primary_failure_is_recoverable(idempotency_key):
        # A NOOP (no messaging backend / user_id configured) is not a
        # transport failure — a fallback over the same unconfigured backend
        # cannot help, so there is nothing to recover and no warning to
        # raise. The fallback transport exists only for the #1173 class:
        # a configured backend whose send genuinely FAILED.
        return NotifyResult(delivered=False, transport=NotifyTransport.NONE)

    logger.warning(
        "notify_with_fallback: primary notify_user delivery failed for key=%s — trying fallback transport",
        idempotency_key,
    )
    return _deliver_via_fallback(
        text,
        kind=kind_value,
        idempotency_key=idempotency_key,
        user_id=user_id,
        linkify=linkify,
    )


def _primary_failure_is_recoverable(idempotency_key: str) -> bool:
    """Whether the primary's recorded miss warrants a fallback attempt.

    The primary :func:`notify_user` writes a ``BotPing`` row before it
    returns ``False``: ``FAILED`` for a configured backend whose send broke
    (the #1173 class the fallback transport is for), ``NOOP`` when nothing
    is configured to send through (a fallback cannot help). Absent a row,
    fall back conservatively — a delivery the agent expected should still
    get the second verified attempt rather than be silently dropped.

    A STALE ``SENDING`` row also warrants a fallback: its owner claimed
    delivery then crashed before finalizing, so nothing landed. Sharing
    :meth:`BotPing.is_stale_sending` keeps the staleness rule identical to
    the one ``claim_delivery`` uses — a fresh SENDING (a genuine concurrent
    in-flight delivery) is NOT recoverable here and still blocks the fallback.
    """
    row = BotPing.objects.filter(idempotency_key=idempotency_key).first()
    if row is None:
        return True
    return row.status == BotPing.Status.FAILED or BotPing.is_stale_sending(row.status, row.posted_at)


def _deliver_via_fallback(
    text: str,
    *,
    kind: NotifyKind,
    idempotency_key: str,
    user_id: str | None,
    linkify: bool,
) -> NotifyResult:
    """Direct, round-trip-verified send used when the primary path fails."""
    primary_failure = "primary notify_user did not deliver"

    backend = messaging_from_overlay()
    resolved_user_id = user_id if user_id is not None else resolve_user_id()
    if backend is None or not resolved_user_id:
        _record_fallback_failure(
            idempotency_key=idempotency_key,
            kind=kind,
            text=text,
            error=f"{primary_failure}; fallback unavailable (no messaging backend or user_id)",
        )
        return NotifyResult(delivered=False, transport=NotifyTransport.NONE)

    payload_text = format_notification(maybe_linkify(text) if linkify else text, kind)
    channel, posted_ts, send_failure = _direct_send(backend, user_id=resolved_user_id, text=payload_text)
    if send_failure:
        _record_fallback_failure(
            idempotency_key=idempotency_key,
            kind=kind,
            text=text,
            error=f"{primary_failure}; fallback send failed: {send_failure}",
        )
        return NotifyResult(delivered=False, transport=NotifyTransport.NONE)

    if not _round_trip_verified(backend, channel=channel, posted_ts=posted_ts):
        _record_fallback_failure(
            idempotency_key=idempotency_key,
            kind=kind,
            text=text,
            error=f"{primary_failure}; fallback send unverified (round-trip read found no message at ts={posted_ts})",
        )
        return NotifyResult(delivered=False, transport=NotifyTransport.NONE)

    send = _DeliveredSend(
        channel=channel,
        posted_ts=posted_ts,
        permalink=_safe_permalink(backend, channel=channel, posted_ts=posted_ts),
    )
    _record_fallback_success(
        idempotency_key=idempotency_key,
        kind=kind,
        text=text,
        send=send,
        primary_failure=primary_failure,
    )
    return NotifyResult(delivered=True, transport=NotifyTransport.FALLBACK)


def _direct_send(
    backend: MessagingBackend,
    *,
    user_id: str,
    text: str,
) -> tuple[str, str, str]:
    """Open a DM and post ``text``, returning ``(channel, ts, failure)``.

    ``failure`` is ``""`` only on a confirmed post (non-empty channel,
    ``ok:true`` with a non-empty ``ts``); otherwise ``(channel, ts)`` must
    not be trusted. Mirrors ``teatree.core.notify._deliver_dm`` — the same
    three Slack non-delivery shapes are hard failures here too.
    """
    try:
        channel = backend.open_dm(user_id)
        if not channel:
            return "", "", "open_dm returned an empty channel (Slack conversations.open ok:false)"
        response = backend.post_message(channel=channel, text=text, thread_ts="")
    except Exception as exc:  # noqa: BLE001 — fallback must never bubble into the turn
        return "", "", str(exc)

    posted_ts = str(response.get("ts", "")) if isinstance(response, dict) else ""
    response_ok = bool(response.get("ok")) if isinstance(response, dict) else False
    if not response_ok or not posted_ts:
        slack_error = str(response.get("error", "")) if isinstance(response, dict) else ""
        detail = f"Slack post failed: {slack_error}" if slack_error else "Slack post returned no message ts"
        return channel, posted_ts, detail
    return channel, posted_ts, ""


def _round_trip_verified(
    backend: MessagingBackend,
    *,
    channel: str,
    posted_ts: str,
) -> bool:
    """Re-read the posted message to confirm it actually landed.

    The verified-delivery gate: a direct send is only ``delivered`` once a
    ``fetch_message`` against the same ``(channel, ts)`` returns a matching
    message. Any read failure or empty result fails closed.
    """
    try:
        message = backend.fetch_message(channel=channel, ts=posted_ts)
    except Exception as exc:  # noqa: BLE001 — a failed verification read fails closed
        logger.warning("notify_with_fallback: round-trip read raised for ts=%s: %s", posted_ts, exc)
        return False
    return bool(message) and str(message.get("ts", "")) == posted_ts


def _safe_permalink(backend: MessagingBackend, *, channel: str, posted_ts: str) -> str:
    try:
        return backend.get_permalink(channel=channel, ts=posted_ts)
    except Exception as exc:  # noqa: BLE001 — permalink lookup is best-effort
        logger.debug("notify_with_fallback: permalink lookup failed for ts=%s: %s", posted_ts, exc)
        return ""


def _record_fallback_success(
    *,
    idempotency_key: str,
    kind: NotifyKind,
    text: str,
    send: _DeliveredSend,
    primary_failure: str,
) -> None:
    """Upsert the BotPing row to SENT via the FALLBACK transport.

    ``notify_user`` already wrote a recoverable FAILED/NOOP row for this
    key on its way out; replace it with the verified-delivered fallback
    outcome, keeping the original primary failure in ``error_message`` so
    #1173 stays diagnosable.
    """
    _upsert_botping(
        idempotency_key=idempotency_key,
        kind=kind,
        status=BotPing.Status.SENT,
        text=text,
        channel_ref=send.channel,
        posted_ts=send.posted_ts,
        permalink=send.permalink,
        transport=BotPing.Transport.FALLBACK,
        error_message=primary_failure,
    )


def _record_fallback_failure(
    *,
    idempotency_key: str,
    kind: NotifyKind,
    text: str,
    error: str,
) -> None:
    _upsert_botping(
        idempotency_key=idempotency_key,
        kind=kind,
        status=BotPing.Status.FAILED,
        text=text,
        error_message=error,
    )


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _upsert_botping(  # noqa: PLR0913 — one typed write site for the BotPing audit row; each field is explicit.
    *,
    idempotency_key: str,
    kind: NotifyKind,
    status: BotPing.Status,
    text: str,
    error_message: str,
    channel_ref: str = "",
    posted_ts: str = "",
    permalink: str = "",
    transport: BotPing.Transport = BotPing.Transport.UNSET,
) -> None:
    try:
        with transaction.atomic():
            BotPing.objects.update_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    "kind": kind.value,
                    "status": status,
                    "text": text,
                    "channel_ref": channel_ref,
                    "posted_ts": posted_ts,
                    "permalink": permalink,
                    "transport": transport.value,
                    "error_message": error_message,
                },
            )
    except IntegrityError:
        logger.debug("notify_with_fallback: BotPing upsert race on key=%s", idempotency_key)
    except DatabaseError:
        logger.warning("notify_with_fallback: BotPing upsert db error on key=%s", idempotency_key)


def _stamp_transport(idempotency_key: str, transport: NotifyTransport) -> None:
    """Record the delivering transport on the row ``notify_user`` just wrote."""
    try:
        with transaction.atomic():
            BotPing.objects.filter(idempotency_key=idempotency_key).update(transport=transport.value)
    except IntegrityError:
        logger.debug("notify_with_fallback: transport stamp race on key=%s", idempotency_key)
    except DatabaseError:
        logger.warning("notify_with_fallback: transport stamp db error on key=%s", idempotency_key)


__all__ = ["NotifyResult", "NotifyTransport", "notify_with_fallback"]
