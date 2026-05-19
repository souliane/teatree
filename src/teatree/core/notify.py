"""Bot→user Slack notification helper — core implementation (#963).

This module owns the actual implementation (BotPing audit + messaging
backend lookup + Slack post). The top-level :mod:`teatree.notify` is a
thin re-export of :func:`notify_user` / :class:`NotifyKind` kept as the
public CLI-facing import; pre-existing callers that already
``from teatree.notify import notify_user`` keep working without churn.

Reason for the split: ``teatree.core`` modules that need to fire a
bot→user DM (notably :mod:`teatree.core.on_behalf_gate_recorded` under
the AUTO_DRAFT verdict — #960) cannot import a top-level
``teatree.notify`` because the tach module graph forbids a
``teatree.core → teatree.notify`` edge (notify itself depends on core,
which would create a cycle). Moving the implementation into core keeps
the dependency direction one-way.
"""

import enum
import logging
import re

from django.db import IntegrityError, transaction

from teatree.backends.protocols import MessagingBackend
from teatree.config import get_effective_settings, load_config
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.models import BotPing
from teatree.slack_mrkdwn import slack_linkify

logger = logging.getLogger(__name__)

# Idempotency-key convention for replies to a Slack-DM question (#1063):
# ``answer-<anything>-<slack_ts>``. ``slack_ts`` is the Slack message
# timestamp (e.g. ``1700000000.0001``) of the question the agent is
# answering. When notify_user sees a key with this shape it auto-stamps
# ``answered_at`` on the matching :class:`PendingChatInjection` row, so
# the Stop hook stops nagging once the reply has been posted.
_ANSWER_KEY_PATTERN = re.compile(r"^answer-.+-(\d+\.\d+)$")


class NotifyKind(enum.StrEnum):
    """Direction of the bot→user notification."""

    ANSWER = "answer"
    QUESTION = "question"
    INFO = "info"


def notify_user(  # noqa: PLR0913 — single notification egress; each kwarg is a documented opt-in / test override.
    text: str,
    *,
    kind: NotifyKind | str,
    idempotency_key: str,
    backend: MessagingBackend | None = None,
    user_id: str | None = None,
    linkify: bool = True,
    answering_slack_ts: str = "",
) -> bool:
    """Send a bot→user Slack DM and record an audit row.

    See :mod:`teatree.notify` for the full docstring — this is the
    canonical implementation; the public module is a re-export.

    ``answering_slack_ts`` (#1063): when this DM is the agent's reply to
    a queued user-question (the user DM'd, the question was injected via
    :class:`PendingChatInjection`, the agent is now replying), pass the
    Slack ``ts`` of that question. The matching row(s) get their
    ``answered_at`` stamped so the Stop hook stops nagging. Alternatively
    use an idempotency-key of the form ``answer-<anything>-<slack_ts>``
    and the same auto-stamp triggers — useful for callers that don't
    plumb the explicit kwarg through.
    """
    kind_value = NotifyKind(kind) if not isinstance(kind, NotifyKind) else kind

    if not _feature_enabled():
        logger.debug("notify_user disabled by settings — %s skipped", idempotency_key)
        return False

    existing = BotPing.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        logger.debug("notify_user idempotent no-op for key=%s", idempotency_key)
        return existing.status == BotPing.Status.SENT

    resolved_backend = backend if backend is not None else messaging_from_overlay()
    resolved_user_id = user_id if user_id is not None else _resolve_user_id()

    if resolved_backend is None or not resolved_user_id:
        _record_noop(
            idempotency_key=idempotency_key,
            kind=kind_value,
            text=text,
            reason="no messaging backend or user_id configured",
        )
        return False

    payload_text = _maybe_linkify(text) if linkify else text

    try:
        channel = resolved_backend.open_dm(resolved_user_id)
        response = resolved_backend.post_message(
            channel=channel,
            text=_format(payload_text, kind_value),
            thread_ts="",
        )
    except Exception as exc:  # noqa: BLE001 — notify must never bubble up
        logger.warning("notify_user transport failed for key=%s: %s", idempotency_key, exc)
        _record_failed(
            idempotency_key=idempotency_key,
            kind=kind_value,
            text=text,
            error=str(exc),
        )
        return False

    posted_ts = str(response.get("ts", "")) if isinstance(response, dict) else ""
    permalink = ""
    if channel and posted_ts:
        try:
            permalink = resolved_backend.get_permalink(channel=channel, ts=posted_ts)
        except Exception as exc:  # noqa: BLE001 — permalink lookup is best-effort
            logger.debug("notify_user permalink lookup failed for key=%s: %s", idempotency_key, exc)
            permalink = ""
    try:
        with transaction.atomic():
            BotPing.objects.create(
                idempotency_key=idempotency_key,
                kind=kind_value.value,
                status=BotPing.Status.SENT,
                text=text,
                channel_ref=str(channel),
                posted_ts=posted_ts,
                permalink=permalink,
            )
    except IntegrityError:
        logger.debug("notify_user race on key=%s — already audited", idempotency_key)

    _maybe_stamp_answered(
        idempotency_key=idempotency_key,
        answering_slack_ts=answering_slack_ts,
    )
    return True


def _maybe_stamp_answered(*, idempotency_key: str, answering_slack_ts: str) -> None:
    """Auto-stamp ``PendingChatInjection.answered_at`` when this DM is a reply (#1063).

    Two trigger forms, both consulted (an explicit kwarg wins over the
    pattern-match — the kwarg is the canonical, programmatic form). One:
    ``answering_slack_ts="1700000000.0001"`` — the caller explicitly
    passes the Slack ts of the question being answered. Two:
    ``idempotency_key="answer-<anything>-<slack_ts>"`` — the agent used
    the answer-key convention; the ts is extracted from the suffix.

    The active overlay (``T3_OVERLAY_NAME``) scopes the stamp to its own
    queue; the empty overlay (the default in the single-overlay v1 path)
    matches the empty-overlay rows the scanner records under.
    """
    ts = answering_slack_ts
    if not ts:
        match = _ANSWER_KEY_PATTERN.match(idempotency_key)
        if match is None:
            return
        ts = match.group(1)
    # Deferred import (mirrors ``_resolve_user_id`` / ``_maybe_linkify``
    # in this module): the answer-stamp is an opt-in side path; keeping
    # the model + os imports out of ``teatree.core.notify`` import time
    # avoids perturbing the module-import graph that the on-behalf gate
    # and notify suites rely on.
    import os  # noqa: PLC0415

    from teatree.core.models import PendingChatInjection  # noqa: PLC0415

    overlay = os.environ.get("T3_OVERLAY_NAME", "")
    try:
        PendingChatInjection.agent_answered_question(ts, overlay=overlay)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break notify_user
        logger.debug("notify_user answered_at stamp failed for ts=%s: %s", ts, exc)


def _feature_enabled() -> bool:
    """Read ``notify_user_via_bot`` from the active settings (default ``True``)."""
    settings_ = get_effective_settings()
    return bool(getattr(settings_, "notify_user_via_bot", True))


def _resolve_user_id() -> str:
    """Resolve the Slack user id to DM (overlay override → global → empty).

    Mirrors ``backend_factory._messaging_from_toml`` (which reads the
    same ``slack_user_id`` key off the overlay table) so a single global
    fallback isn't required — every routing path agrees on the same
    resolution order.
    """
    import os  # noqa: PLC0415

    cfg = load_config().raw
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
    overlays = cfg.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    teatree_cfg = cfg.get("teatree") or {}
    return str(teatree_cfg.get("slack_user_id", ""))


def _maybe_linkify(text: str) -> str:
    """Apply :func:`slack_linkify` using the active overlay's resolvers, if any.

    Failure to resolve the overlay or to query a resolver is non-fatal —
    notification routing must never crash a CLI turn. In that case the
    text is rewritten with no resolvers, which still converts
    ``[label](url)`` to ``<url|label>`` but leaves bare ``!N`` / ``#N``
    tokens as-is.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001 — overlay resolution is best-effort; never crash a CLI turn
        return slack_linkify(text)
    return slack_linkify(
        text,
        mr_resolver=overlay.resolve_mr_token,
        issue_resolver=overlay.resolve_issue_token,
    )


def _format(text: str, kind: NotifyKind) -> str:
    """Prefix the DM with a kind marker for easy scan-reading on mobile."""
    prefix = {
        NotifyKind.ANSWER: ":speech_balloon: *answer*",
        NotifyKind.QUESTION: ":question: *question*",
        NotifyKind.INFO: ":information_source: *info*",
    }[kind]
    return f"{prefix}\n{text}"


def _record_noop(*, idempotency_key: str, kind: NotifyKind, text: str, reason: str) -> None:
    try:
        with transaction.atomic():
            BotPing.objects.create(
                idempotency_key=idempotency_key,
                kind=kind.value,
                status=BotPing.Status.NOOP,
                text=text,
                error_message=reason,
            )
    except IntegrityError:
        logger.debug("notify_user noop race on key=%s", idempotency_key)


def _record_failed(*, idempotency_key: str, kind: NotifyKind, text: str, error: str) -> None:
    try:
        with transaction.atomic():
            BotPing.objects.create(
                idempotency_key=idempotency_key,
                kind=kind.value,
                status=BotPing.Status.FAILED,
                text=text,
                error_message=error,
            )
    except IntegrityError:
        logger.debug("notify_user failed-row race on key=%s", idempotency_key)


__all__ = ["NotifyKind", "notify_user"]
