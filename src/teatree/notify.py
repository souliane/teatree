"""Bot→user Slack notification helper (#963).

The user does not read the Claude CLI: answers, questions, and important
info the agent surfaces inside a CLI turn are invisible to them. This
helper provides a single, always-on egress for those three directions —
post as the **bot** to the user's DM (the same channel ``DailyDigest``
opens) so the message arrives in Slack outside the active session.

Out of scope of the on-behalf gates (#960 ``ask_before_post_on_behalf``
and #949 ``notify_on_post_on_behalf``): those govern posts the agent
makes *as the user* to a colleague/customer surface. ``notify_user`` is
the **bot** talking to its own operator — a different concern with a
different doctrine.

Usage:

.. code-block:: python

    from teatree.notify import notify_user, NotifyKind

    notify_user(
        "Backend tests are green on s-963; ready for review.",
        kind=NotifyKind.INFO,
        idempotency_key=f"session={sid};turn={n}",
    )

Returns ``True`` when the bot posted (or detected an idempotent
re-send), ``False`` when no bot is configured (no-op-safe; never raises
into the CLI turn).
"""

import enum
import logging

from django.db import IntegrityError, transaction

from teatree.backends.protocols import MessagingBackend
from teatree.config import get_effective_settings, load_config
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.models import BotPing
from teatree.slack_mrkdwn import slack_linkify

logger = logging.getLogger(__name__)


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
) -> bool:
    """Send a bot→user Slack DM and record an audit row.

    The single egress for the three "agent → user" directions named in
    #963: answering the user, asking the user, surfacing important info.
    It posts as the bot to the user's DM channel via ``open_dm`` +
    ``post_message`` — exactly like ``DailyDigest``, but standalone (no
    daily thread coupling) and audited in its own ``BotPing`` ledger.

    *idempotency_key* MUST uniquely identify the logical send (session
    id + turn index works); a retried turn with the same key is a no-op.

    *backend* and *user_id* are explicit overrides used by tests and by
    callers that want to target a different overlay's backend. When
    omitted, both resolve from the active overlay via
    :func:`messaging_from_overlay` and the ``slack_user_id`` setting on
    the active ``[overlays.<name>]`` table (falling back to the global
    ``[teatree] slack_user_id``).

    Wrapper scripts that need to route a DM to a specific overlay's bot
    (e.g. a post-session helper that wants overlay X's bot rather than
    the one inferred from the current ``T3_OVERLAY_NAME``) should pass
    *backend* explicitly using :func:`messaging_from_overlay(overlay_name=...)`:

    .. code-block:: python

        from teatree.core.backend_factory import messaging_from_overlay
        from teatree.notify import notify_user, NotifyKind

        backend = messaging_from_overlay(overlay_name="my-overlay")
        notify_user(
            "ready for review",
            kind=NotifyKind.INFO,
            idempotency_key="session=...;turn=...",
            backend=backend,
            user_id="UXXXX",
        )

    The factory falls back to ``[overlays.<name>]`` in ``~/.teatree.toml``
    when an overlay has no registered Python class, so path-only overlays
    route to their own bot instead of silently returning ``None``.

    On success, fetches the message permalink via ``get_permalink`` and
    stores it on the ``BotPing`` row so subsequent CLI surfaces can echo
    a clickable link back to the user (the audited DM is the canonical
    source of the answer; the CLI only points at it).

    *linkify* (default ``True``) rewrites GitHub-flavored ``[label](url)``
    and bare ``!N`` / ``#N`` tokens into Slack mrkdwn ``<url|label>`` form
    so dashboard messages render with clickable links rather than inert
    text. Token resolution consults the active overlay's
    :meth:`OverlayBase.resolve_mr_token` /
    :meth:`OverlayBase.resolve_issue_token` hooks; tokens the overlay
    can't resolve are left bare. The transform is applied to the Slack
    payload only — the ``BotPing.text`` audit column keeps the original.

    Returns ``True`` when a Slack message was published (or detected as
    an idempotent repeat); ``False`` when the helper degraded to a no-op
    (no backend configured, no user id, feature toggled off, or
    transport raised). The helper never raises — a CLI turn must never
    crash because notification routing failed.
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
    return True


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
