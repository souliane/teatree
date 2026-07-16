"""Bot→user Slack notification helper (#963).

The user does not read the Claude CLI: answers, questions, and important
info the agent surfaces inside a CLI turn are invisible to them. This
helper is the single, always-on egress for those directions — post as the
**bot** to the user's DM (the same channel ``DailyDigest`` opens) so the
message arrives in Slack outside the active session. It owns the whole
path: BotPing audit + messaging backend lookup + Slack post.

Living in ``teatree.core`` keeps the dependency direction one-way — the
core modules that fire a bot→user DM (:mod:`teatree.core.on_behalf_gate_recorded`
under the AUTO_DRAFT verdict #960, and :mod:`teatree.core.on_behalf_post_receipt`
for the after-receipt visibility DM, the default-ON ``notify_on_post_on_behalf``
``UserSettings`` field #949) import it as a core sibling with no cycle.

Out of scope of the on-behalf concerns (#960 ``on_behalf_post_mode``,
#949 ``notify_on_post_on_behalf``): those govern posts the agent makes
*as the user* to a colleague/customer surface. ``notify_user`` itself is
the **bot** talking to its own operator — a different concern with a
different doctrine.

Returns ``True`` when the bot posted (or detected an idempotent
re-send), ``False`` when no bot is configured (no-op-safe; never raises
into the CLI turn).
"""

import enum
import logging
import os
import re

from django.db import DatabaseError, IntegrityError, transaction

from teatree.config import get_effective_settings, load_config
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import BotPing, DeliveryClaim, OutboundClaim
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.send_proxy import SendChannel, SendRequest, route_send
from teatree.core.session_identity import current_session_id
from teatree.slack_mrkdwn import normalize_slack_message, slack_linkify
from teatree.types import RawAPIDict

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


# ast-grep-ignore: ac-django-no-complexity-suppressions
def notify_user(  # noqa: PLR0913 — single notification egress; each kwarg is a documented opt-in / test override.
    text: str,
    *,
    kind: NotifyKind | str,
    idempotency_key: str,
    audience: NotifyAudience,
    backend: MessagingBackend | None = None,
    user_id: str | None = None,
    linkify: bool = True,
    answering_slack_ts: str = "",
    blocks: list[RawAPIDict] | None = None,
) -> bool:
    """Send a bot→user Slack DM and record an audit row.

    See this module's docstring for the bot→user egress contract.

    ``audience`` (:mod:`teatree.core.modelkit.notify_policy`) is REQUIRED — every call
    site declares who the DM is for (deny-by-default). An
    :attr:`~teatree.core.modelkit.notify_policy.NotifyAudience.INTERNAL` notification
    short-circuits BEFORE any backend resolution: it is logged, a terminal
    :attr:`BotPing.Status.LOGGED` row is recorded, and ``False`` is returned —
    no DM ever leaves the machine. Only the four owner audiences reach Slack.

    ``blocks`` (#1777): opaque Block Kit blocks (e.g. a native ``table`` block
    from :mod:`teatree.backends.slack.table_format`) posted alongside ``text``.
    ``text`` remains the notification + degradation fallback, so a caller with
    tabular data passes the monospace fence as ``text`` and the ``table`` block
    here. Kept opaque (``list[RawAPIDict]``) so ``teatree.core`` never imports
    the Slack backend and cycles the module graph.

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

    early = _preflight_result(audience, idempotency_key, kind=kind_value, text=text)
    if early is not None:
        return early

    already = _already_sent_noop(idempotency_key)
    if already is not None:
        return already

    resolved_backend = backend if backend is not None else messaging_from_overlay()
    resolved_user_id = user_id if user_id is not None else resolve_user_id()

    if resolved_backend is None or not resolved_user_id:
        _record_noop(
            idempotency_key=idempotency_key,
            kind=kind_value,
            text=text,
            audience=audience,
            reason="no messaging backend or user_id configured",
        )
        return False

    gate = _claim_delivery_slot(idempotency_key, kind=kind_value.value, text=text, audience=audience.value)
    if gate is not None:
        return gate

    _route_through_send_proxy(text, destination=resolved_user_id)

    payload_text = maybe_linkify(text) if linkify else text

    channel, posted_ts, failure = _deliver_dm(
        resolved_backend,
        user_id=resolved_user_id,
        text=format_notification(payload_text, kind_value),
        blocks=blocks,
    )
    if failure:
        # Any non-delivery — empty channel from ``open_dm`` (Slack
        # ``conversations.open ok:false``), a transport exception, a
        # ``post_message`` ``ok:false``, or an ``ok:true`` with no
        # ``ts`` — is a HARD FAILURE. The claimed SENDING row is stamped
        # FAILED so a later retry under the same key recovers it (#1306).
        logger.warning("notify_user delivery failed for key=%s: %s", idempotency_key, failure)
        _finalize_failed(idempotency_key=idempotency_key, error=failure)
        return False
    # ``channel`` and ``posted_ts`` are both non-empty here — ``_deliver_dm``
    # only returns no failure when both are set, so no defensive re-check.
    try:
        permalink = resolved_backend.get_permalink(channel=channel, ts=posted_ts)
    except Exception as exc:  # noqa: BLE001 — permalink lookup is best-effort
        logger.debug("notify_user permalink lookup failed for key=%s: %s", idempotency_key, exc)
        permalink = ""
    _finalize_sent(
        idempotency_key=idempotency_key,
        channel=str(channel),
        posted_ts=posted_ts,
        permalink=permalink,
    )

    _maybe_stamp_answered(
        idempotency_key=idempotency_key,
        answering_slack_ts=answering_slack_ts,
    )
    _record_outbound_claim(
        idempotency_key=f"slack_dm:{idempotency_key}",
        target_url=permalink,
        channel=str(channel),
        posted_ts=posted_ts,
    )
    return True


def _preflight_result(audience: NotifyAudience, idempotency_key: str, *, kind: NotifyKind, text: str) -> bool | None:
    """Resolve the pre-delivery short-circuits — ``False`` to stop, ``None`` to proceed.

    An INTERNAL audience is logged and terminally recorded (never DM'd, deny-by-
    default), and a settings-disabled feature is skipped — both return the
    early-exit ``False``. ``None`` means the notification is owner-audience and
    enabled, so delivery proceeds.
    """
    if audience == NotifyAudience.INTERNAL:
        logger.info("notify_user INTERNAL (log-only, not DM'd) key=%s: %s", idempotency_key, text[:120])
        BotPing.record_logged(idempotency_key, kind=kind.value, text=text, audience=audience.value)
        return False
    if not _feature_enabled():
        logger.debug("notify_user disabled by settings — %s skipped", idempotency_key)
        return False
    return None


def _already_sent_noop(idempotency_key: str) -> bool | None:
    """Fast SENT-idempotency no-op: ``True`` if already delivered, else ``None``.

    An already-delivered key is a no-op even when the backend is now
    unconfigured (the prior shape checked SENT before resolving the
    backend). A read-only lookup; the atomic ``claim_delivery`` is still the
    authoritative double-DM gate. ``None`` means "not yet SENT — proceed".
    A ``DatabaseError`` fails closed (``False``) — never escapes the
    never-raise contract.
    """
    try:
        if BotPing.objects.filter(idempotency_key=idempotency_key, status=BotPing.Status.SENT).exists():
            logger.debug("notify_user idempotent no-op for key=%s", idempotency_key)
            return True
    except DatabaseError as exc:
        logger.warning("notify_user idempotency-ledger read failed for key=%s: %s", idempotency_key, exc)
        return False
    return None


def _claim_delivery_slot(idempotency_key: str, *, kind: str, text: str, audience: str = "") -> bool | None:
    """Atomically claim the right to deliver — the double-DM TOCTOU gate.

    Returns ``None`` when this caller won the claim and must proceed to
    deliver; otherwise the early-exit result for ``notify_user``: ``True``
    for an already-SENT idempotent no-op, ``False`` when a concurrent tick
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
    try:
        claim = BotPing.claim_delivery(idempotency_key, kind=kind, text=text, audience=audience)
    except DatabaseError as exc:
        logger.warning("notify_user delivery-claim access failed for key=%s: %s", idempotency_key, exc)
        return False
    if claim == DeliveryClaim.CLAIMED:
        return None
    if claim == DeliveryClaim.ALREADY_SENT:
        logger.debug("notify_user idempotent no-op for key=%s", idempotency_key)
        return True
    logger.info("notify_user delivery already claimed by a concurrent tick for key=%s", idempotency_key)
    return False


def _maybe_stamp_answered(*, idempotency_key: str, answering_slack_ts: str) -> None:
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
    # Deferred import (mirrors ``resolve_user_id`` / ``maybe_linkify``
    # in this module): the answer-stamp is an opt-in side path; keeping
    # the model import out of ``teatree.core.notify`` import time avoids
    # perturbing the module-import graph that the on-behalf gate and
    # notify suites rely on.
    from teatree.core.models import PendingChatInjection  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        PendingChatInjection.agent_answered_question(ts)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break notify_user
        logger.debug("notify_user answered_at stamp failed for ts=%s: %s", ts, exc)


def _active_dm_thread(channel: str) -> str:
    from teatree.core.models import IncomingEvent  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        return IncomingEvent.objects.active_dm_thread(channel=channel)
    except DatabaseError as exc:
        logger.debug("active_dm_thread lookup failed for channel=%s: %s", channel, exc)
        return ""


def _deliver_dm(
    backend: MessagingBackend,
    *,
    user_id: str,
    text: str,
    blocks: list[RawAPIDict] | None = None,
) -> tuple[str, str, str]:
    """Open a DM and post ``text``, returning ``(channel, ts, failure)``.

    ``failure`` is ``""`` on a confirmed delivery (non-empty channel,
    ``ok:true`` response with a non-empty ``ts``). Otherwise it holds a
    human-readable reason and ``(channel, ts)`` must NOT be trusted —
    every non-empty ``failure`` is a HARD FAILURE for the caller.

    The three non-delivery shapes Slack produces, all previously treated
    as benign successes, are:
    (a) ``open_dm`` returns ``""`` — ``conversations.open ok:false``
    (missing scope, user not found); posting to ``""`` silently no-ops.
    (b) ``post_message`` raises — transport/network error.
    (c) ``post_message`` returns ``ok:false`` (missing_scope,
    channel_not_found) or ``ok:true`` with no ``ts`` — nothing landed.

    **Why ``post_message`` instead of ``deliver_user_dm`` (#2054):** the
    speak chokepoint (:func:`teatree.core.speak.deliver_user_dm`) posts
    the text body via ``post_audio_dm`` (``files.getUploadURLExternal`` +
    ``files.completeUploadExternal``) when ``speak.slack`` is on and
    synthesis succeeds. That response does NOT carry a ``ts`` at the top
    level — Slack populates it only under
    ``files[].shares.private.<channel>[].ts``, which is frequently absent.
    Reading ``response.get("ts", "")`` therefore always returns ``""`` and
    every delivery fails with "Slack post returned no message ts".

    The fix: use ``backend.post_message`` (``chat.postMessage``) for the
    canonical text delivery — it always returns ``{"ok": true, "ts": "…"}``
    — and fire the speak side-effects (audio attachment + local playback)
    independently via ``deliver_user_dm_sidecar`` so the DM and its audio
    enrichment are independent concerns. A failure on the speak side never
    suppresses the text delivery.
    """
    from teatree.core.speak import deliver_user_dm_sidecar  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        channel = backend.open_dm(user_id)
        if not channel:
            return "", "", "open_dm returned an empty channel (Slack conversations.open ok:false)"
        thread_ts = _active_dm_thread(channel)
        # Pass ``blocks`` only when a table is actually present — the common
        # text-only path stays a 3-arg call, so any backend (or test double)
        # that predates the ``blocks`` kwarg keeps working unchanged.
        if blocks is None:
            response = backend.post_message(channel=channel, text=text, thread_ts=thread_ts)
        else:
            response = backend.post_message(channel=channel, text=text, thread_ts=thread_ts, blocks=blocks)
        deliver_user_dm_sidecar(backend, channel=channel, text=text, thread_ts=thread_ts)
    except Exception as exc:  # noqa: BLE001 — notify must never bubble up
        return "", "", str(exc)

    posted_ts = str(response.get("ts", "")) if isinstance(response, dict) else ""
    response_ok = bool(response.get("ok")) if isinstance(response, dict) else False
    if not response_ok or not posted_ts:
        slack_error = str(response.get("error", "")) if isinstance(response, dict) else ""
        detail = f"Slack post failed: {slack_error}" if slack_error else "Slack post returned no message ts"
        return channel, posted_ts, detail
    return channel, posted_ts, ""


def _record_outbound_claim(
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


def _route_through_send_proxy(text: str, *, destination: str) -> None:
    """Audit this bot→user DM through the #117 send-proxy (self-DM, never gated).

    ``notify_user`` is the bot talking to its own operator, so the DM is a
    self-destination (``is_self_dm=True``): the proxy records a ``SendAudit`` row
    but never blocks it or redacts it (the never-lockout carve-out — the user
    must see everything the bot sends them). Best-effort: the route is guarded so
    a proxy failure can never break the notify path.
    """
    try:
        route_send(
            SendRequest(
                channel=SendChannel.SLACK,
                destination=destination,
                payload=text,
                action="notify_user",
                is_self_dm=True,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — send-proxy audit is a side path; never break notify.
        logger.debug("notify_user send-proxy audit failed: %s", exc)


def _feature_enabled() -> bool:
    """Read ``notify_user_via_bot`` from the active settings (default ``True``)."""
    settings_ = get_effective_settings()
    return bool(getattr(settings_, "notify_user_via_bot", True))


def resolve_user_id() -> str:
    """Resolve the Slack user id to DM (overlay override → global → empty).

    The per-overlay id comes from the DB overlays registry (still injected into
    ``load_config().raw["overlays"]``); the GLOBAL fallback reads the DB-home
    ``slack_user_id`` setting so every routing path agrees on the same order.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: call-time import, kept lazy

    cfg = load_config().raw
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
    overlays = cfg.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    return cold_reader.str_setting("slack_user_id", default="")


def resolve_user_channel() -> str:
    """Resolve the Slack DM channel id the user reads (overlay override → global → empty).

    The canonical resolver for the ``slack_user_channel`` config key,
    walking the SAME overlay→global→empty order :func:`resolve_user_id`
    uses for ``slack_user_id``. Both DM-channel call sites (the bot→user
    DM path and the live-post-approval CLI verifier) consult this single
    helper, so a change to the resolution order can never drift between
    two private copies (the config-trap the #126 redesign closes).

    An empty return means no channel is configured; the caller treats it
    as "open a DM to the resolved user_id" rather than pinning to a
    specific ``D...`` channel.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: call-time import, kept lazy

    cfg = load_config().raw
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
    overlays = cfg.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        channel = overlays[overlay_name].get("slack_user_channel", "")
        if channel:
            return str(channel)
    return cold_reader.str_setting("slack_user_channel", default="")


def maybe_linkify(text: str) -> str:
    """Apply :func:`slack_linkify` using the active overlay's resolvers, if any.

    Failure to resolve the overlay or to query a resolver is non-fatal —
    notification routing must never crash a CLI turn. In that case the
    text is rewritten with no resolvers, which still converts
    ``[label](url)`` to ``<url|label>`` but leaves bare ``!N`` / ``#N``
    tokens as-is.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001 — overlay resolution is best-effort; never crash a CLI turn
        return slack_linkify(text)
    return slack_linkify(
        text,
        mr_resolver=overlay.resolve_mr_token,
        issue_resolver=overlay.resolve_issue_token,
    )


def format_notification(text: str, kind: NotifyKind) -> str:
    """Prefix the DM with a kind marker for easy scan-reading on mobile."""
    prefix = {
        NotifyKind.ANSWER: ":speech_balloon: *answer*",
        NotifyKind.QUESTION: ":question: *question*",
        NotifyKind.INFO: ":information_source: *info*",
    }[kind]
    return f"{prefix}\n{normalize_slack_message(text)}"


def _record_noop(*, idempotency_key: str, kind: NotifyKind, text: str, audience: NotifyAudience, reason: str) -> None:
    try:
        with transaction.atomic():
            BotPing.objects.create(
                idempotency_key=idempotency_key,
                kind=kind.value,
                status=BotPing.Status.NOOP,
                text=text,
                audience=audience.value,
                error_message=reason,
            )
    except IntegrityError:
        logger.debug("notify_user noop race on key=%s", idempotency_key)
    except DatabaseError as exc:
        logger.warning("notify_user noop audit write failed for key=%s: %s", idempotency_key, exc)


def _finalize_sent(*, idempotency_key: str, channel: str, posted_ts: str, permalink: str) -> None:
    """Stamp the claimed SENDING row terminal-SENT — never raises (the DM has landed)."""
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
        # contract). Mirror ``_record_outbound_claim``.
        logger.warning("notify_user sent-finalize write failed for key=%s: %s", idempotency_key, exc)


def _finalize_failed(*, idempotency_key: str, error: str) -> None:
    """Stamp the claimed SENDING row terminal-FAILED so a later retry recovers it."""
    try:
        BotPing.finalize_failed(idempotency_key, error=error)
    except DatabaseError as exc:
        logger.warning("notify_user failed-finalize write failed for key=%s: %s", idempotency_key, exc)


def drain_undelivered_notifies(
    *, user_id: str = "", overlay: str = "", backend: MessagingBackend | None = None, limit: int = 50
) -> tuple[int, int]:
    """Re-deliver INFO DMs that stranded with no reachable backend.

    The cross-tick re-delivery peer of the
    :mod:`teatree.core.notify_question_drains` QUESTION drains. A
    bot→user INFO DM fired from a sub-agent shell whose restricted PATH
    cannot read ``pass`` resolves no messaging backend, so :func:`notify_user`
    records a recoverable NOOP :class:`BotPing` row and returns ``False`` — the
    DM is durably parked, not lost. This drain runs on a later tick in a
    context that *does* have a working backend (the orchestrator loop) and
    re-attempts each parked INFO row under its original ``idempotency_key``.

    Bounded so it can never grind forever (#2064). Before attempting any
    delivery the drain :meth:`BotPing.expire_stale_info` terminally EXPIRES
    rows past :attr:`BotPing.REDELIVERY_AGE_CUTOFF` (stale operator noise that
    must never surface late) or past :attr:`BotPing.MAX_REDELIVERY_ATTEMPTS`
    (the per-row attempt cap). For each remaining recoverable row it re-runs
    :func:`notify_user`; a row that did NOT deliver (no backend resolved in
    this context) has its :attr:`BotPing.attempts` bumped so it converges on
    the cap instead of silently re-recording the same NOOP under its unique key
    every tick — the root cause of the no-op grind.

    Distinct from the :func:`teatree.messaging.notify_with_fallback` NOOP rule
    ("a NOOP is not recoverable"): that wrapper retries the *same* call within
    one turn over the *same* unconfigured backend, where a NOOP genuinely
    cannot recover. This drain retries on a *different tick* in a *different*
    context whose backend resolves — the only place a NOOP becomes deliverable.

    Re-delivery is via :func:`notify_user` under the parked key, so
    :meth:`BotPing.claim_delivery` replaces the recoverable row with a fresh
    claim and the existing idempotency/audit invariants hold unchanged. Fails
    open: one row's delivery failure (re-recorded on its own row) never aborts
    the drain or raises. Returns ``(delivered, total)`` over the rows attempted.
    """
    BotPing.expire_stale_info()

    rows = list(BotPing.recoverable_info(limit=limit))
    if not rows:
        return 0, 0

    previous_overlay = os.environ.get("T3_OVERLAY_NAME")
    if overlay:
        os.environ["T3_OVERLAY_NAME"] = overlay
    delivered = 0
    try:
        for row in rows:
            # ``recoverable_info`` only returns owner-audience rows, so the
            # stored audience is always one the owner reads — re-declare it so
            # the re-delivery does not fall foul of the deny-by-default gate.
            if notify_user(
                row.text,
                kind=NotifyKind.INFO,
                idempotency_key=row.idempotency_key,
                audience=NotifyAudience(row.audience),
                backend=backend,
                user_id=user_id or None,
            ):
                delivered += 1
            else:
                BotPing.bump_attempt(row.idempotency_key)
    finally:
        if overlay:
            if previous_overlay is None:
                os.environ.pop("T3_OVERLAY_NAME", None)
            else:
                os.environ["T3_OVERLAY_NAME"] = previous_overlay

    BotPing.expire_stale_info()

    return delivered, len(rows)


__all__ = ["NotifyKind", "drain_undelivered_notifies", "notify_user"]
