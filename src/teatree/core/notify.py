"""Bot-to-operator Slack notification egress with explicit delivery outcomes."""

import json
import logging
import os
from collections.abc import Callable

from teatree.core.backend_factory import OwnerMessagingTransport, messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.egress_transport import EgressKind, suppressed_egress_response
from teatree.core.modelkit.dm_channel_policy import DmChannel, classify
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify_ledger import (
    already_sent_noop,
    claim_delivery_slot,
    finalize_failed,
    finalize_sent,
    maybe_stamp_answered,
    record_noop,
    record_outbound_claim,
    record_pulled,
)
from teatree.core.notify_targets import (
    Addressee,
    label_fallback,
    resolve_ticket_addressee,
    resolve_user_channel,
    resolve_user_id,
)
from teatree.core.notify_types import (
    DEFAULT_NOTIFY_OPTIONS,
    DELIVERED,
    NotifyKind,
    NotifyOptions,
    NotifyOutcome,
    NotifyReason,
    blocked,
)
from teatree.core.send_proxy import SendChannel, SendRequest, SendVerdict, route_send
from teatree.slack_mrkdwn import normalize_slack_message, slack_linkify
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


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
    """Whether the bot→user DM landed — the boolean face of :func:`notify_user_outcome`.

    Kept for the call sites that only branch on delivered-or-not. A caller that
    must report, retry, or escalate on a non-delivery calls
    :func:`notify_user_outcome` instead and reads its :class:`NotifyReason`.
    """
    return notify_user_outcome(
        text,
        kind=kind,
        idempotency_key=idempotency_key,
        audience=audience,
        options=NotifyOptions(
            backend=backend,
            user_id=user_id,
            linkify=linkify,
            answering_slack_ts=answering_slack_ts,
            blocks=blocks,
        ),
    ).sent


def notify_user_outcome(
    text: str,
    *,
    kind: NotifyKind | str,
    idempotency_key: str,
    audience: NotifyAudience,
    options: NotifyOptions = DEFAULT_NOTIFY_OPTIONS,
) -> NotifyOutcome:
    """Send a bot→user Slack DM and record an audit row.

    See this module's docstring for the bot→user egress contract.

    ``audience`` (:mod:`teatree.core.modelkit.notify_policy`) is REQUIRED — every call
    site declares who the DM is for (deny-by-default). An
    :attr:`~teatree.core.modelkit.notify_policy.NotifyAudience.INTERNAL` notification
    short-circuits BEFORE any backend resolution: it is logged, a terminal
    :attr:`BotPing.Status.LOGGED` row is recorded, and a not-sent
    :class:`NotifyOutcome` is returned — no DM ever leaves the machine. Only the
    four owner audiences reach Slack.

    Every return names its :class:`NotifyReason`, so a caller that cannot deliver
    can say WHY instead of propagating a bare ``False`` that nobody can act on.

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
    ``answered_at`` stamped. Alternatively
    use an idempotency-key of the form ``answer-<anything>-<slack_ts>``
    and the same auto-stamp triggers — useful for callers that don't
    plumb the explicit kwarg through.
    """
    kind_value = NotifyKind(kind) if not isinstance(kind, NotifyKind) else kind

    early = _preflight_result(
        audience, idempotency_key, kind=kind_value, text=text, requested_push=options.requested_push
    ) or already_sent_noop(idempotency_key)
    if early is not None:
        return early

    resolved_backend, backend_refusal = (
        (options.backend, NotifyReason.NONE) if options.backend is not None else resolve_owner_dm_backend()
    )
    addressee = resolve_ticket_addressee(
        options.ticket_id, owner_id=options.user_id if options.user_id is not None else resolve_user_id()
    )

    # Split, never conflated: the halves have different fixes, and a joint
    # message is what let a green "owner id resolves" doctor line coexist with a
    # dead transport for a full day. The backend half carries its own typed
    # refusal (none-at-all vs several-pick-one) from :func:`resolve_owner_dm_backend`.
    if resolved_backend is None or not addressee.user_id:
        reason = backend_refusal if resolved_backend is None else NotifyReason.NO_USER_ID
        record_noop(
            idempotency_key=idempotency_key,
            kind=kind_value,
            text=text,
            audience=audience,
            reason=reason,
        )
        return blocked(reason)

    gate = claim_delivery_slot(idempotency_key, kind=kind_value.value, text=text, audience=audience.value)
    if gate is not None:
        return gate

    outgoing_text = format_notification(maybe_linkify(text) if options.linkify else text, kind_value)
    outgoing_blocks = options.blocks
    outgoing_text, outgoing_blocks = label_fallback(outgoing_text, outgoing_blocks, addressee.fallback_requester)
    if audience is NotifyAudience.OWNER_QUESTION:
        outgoing_text, outgoing_blocks = _owner_question_payload(outgoing_text, outgoing_blocks, addressee.user_id)

    if proxy_gate := _operator_proxy_gate(outgoing_text, outgoing_blocks, idempotency_key, addressee):
        return proxy_gate

    from teatree.core.owner_dm.ticket_dm_delivery import (  # noqa: PLC0415 — deferred: load ticket routing on delivery
        TicketDmRequest,
        deliver_ticket_dm,
        record_ticket_thread_post_after_delivery,
    )

    if options.ticket_id is not None and not options.thread_ts:
        channel, posted_ts, failure = deliver_ticket_dm(
            TicketDmRequest(
                backend=resolved_backend,
                user_id=addressee.user_id,
                text=outgoing_text,
                blocks=outgoing_blocks,
                ticket_id=options.ticket_id,
                deliver=_deliver_dm,
                question_id=options.question_id,
                fallback_thread_ts=options.fallback_thread_ts,
            )
        )
    else:
        channel, posted_ts, failure = _deliver_dm(
            resolved_backend,
            user_id=addressee.user_id,
            text=outgoing_text,
            blocks=outgoing_blocks,
            thread_ts=options.thread_ts,
        )
    if failure:
        # Any non-delivery — empty channel from ``open_dm`` (Slack
        # ``conversations.open ok:false``), a transport exception, a
        # ``post_message`` ``ok:false``, or an ``ok:true`` with no
        # ``ts`` — is a HARD FAILURE. The claimed SENDING row is stamped
        # FAILED so a later retry under the same key recovers it (#1306).
        logger.error(
            "notify_user DID NOT DELIVER — owner NOT notified. key=%s audience=%s reason=%s: %s",
            idempotency_key,
            audience.value,
            NotifyReason.DELIVERY_FAILED.value,
            failure,
        )
        finalize_failed(idempotency_key=idempotency_key, error=failure)
        return blocked(NotifyReason.DELIVERY_FAILED, error=failure)
    if options.thread_ts:
        record_ticket_thread_post_after_delivery(
            channel=channel,
            thread_ts=options.thread_ts,
            posted_ts=posted_ts,
            idempotency_key=idempotency_key,
        )
    try:
        permalink = resolved_backend.get_permalink(channel=channel, ts=posted_ts)
    except Exception as exc:  # noqa: BLE001 — permalink lookup is best-effort
        logger.debug("notify_user permalink lookup failed for key=%s: %s", idempotency_key, exc)
        permalink = ""
    finalize_sent(
        idempotency_key=idempotency_key,
        channel=str(channel),
        posted_ts=posted_ts,
        permalink=permalink,
    )

    maybe_stamp_answered(
        idempotency_key=idempotency_key,
        answering_slack_ts=options.answering_slack_ts,
    )
    record_outbound_claim(
        idempotency_key=f"slack_dm:{idempotency_key}",
        target_url=permalink,
        channel=str(channel),
        posted_ts=posted_ts,
    )
    return DELIVERED


def _owner_question_payload(
    text: str, blocks: list[RawAPIDict] | None, user_id: str
) -> tuple[str, list[RawAPIDict] | None]:
    mention = f"<@{user_id}>"
    if mention not in text:
        text = f"{mention} {text}"
    if not blocks or mention in repr(blocks):
        return text, blocks

    first = blocks[0]
    first_text = first.get("text")
    if (
        first.get("type") == "section"
        and isinstance(first_text, dict)
        and first_text.get("type") == "mrkdwn"
        and isinstance(first_text.get("text"), str)
    ):
        return text, [{**first, "text": {**first_text, "text": f"{mention} {first_text['text']}"}}, *blocks[1:]]
    return text, [{"type": "section", "text": {"type": "mrkdwn", "text": mention}}, *blocks]


def resolve_owner_dm_backend() -> tuple[MessagingBackend | None, NotifyReason]:
    """Resolve the transport for a bot→owner DM: active overlay → sole credentialed → named refusal.

    The owner is a box-global target, so transport resolution mirrors the
    :func:`resolve_user_id` tier order instead of stopping at the active overlay:
    an active overlay that declares no transport (``messaging_backend = "noop"``)
    must not drop an owner DM that a sibling overlay's working credential can
    deliver. Tier one is the active/ambient :func:`messaging_from_overlay`
    resolution with its truthy noop backend REJECTED (via the ``is_noop``
    capability marker — ``core`` cannot import the concrete class, #1922) —
    handing the noop to the delivery path drops the DM behind a misleading
    Slack-shaped error ("open_dm returned an empty channel"). Tier two falls
    back to the sole registered overlay carrying real credentials
    (:meth:`OwnerMessagingTransport.credentialed_backends` — credentials, not count).

    A refusal names itself so the caller can act on it:
    :attr:`NotifyReason.NO_MESSAGING_BACKEND` when no overlay has a transport,
    :attr:`NotifyReason.AMBIGUOUS_OVERLAY` when several do and none is the
    active one — two different fixes, never conflated. The returned backend is
    never a noop.

    Resolution rides overlay discovery (``ep.load()`` imports overlay modules),
    so a broken entry point can raise arbitrary errors here; the egress's
    never-raise contract turns that into a WARN plus the named
    no-transport refusal — the DM parks as a recoverable NOOP row instead of
    the crash killing the CLI turn.
    """
    try:
        active = messaging_from_overlay()
        # The marker is read off the TYPE: it is a ClassVar on the noop backend,
        # and an instance-level getattr would misread a MagicMock stub's truthy
        # auto-attribute as "noop" and drop a deliverable DM.
        if active is not None and not getattr(type(active), "is_noop", False):
            return active, NotifyReason.NONE
        credentialed = OwnerMessagingTransport.credentialed_backends()
    except Exception:
        logger.warning("owner-DM transport resolution failed — treating as no transport", exc_info=True)
        return None, NotifyReason.NO_MESSAGING_BACKEND
    if len(credentialed) == 1:
        return credentialed[0], NotifyReason.NONE
    if credentialed:
        return None, NotifyReason.AMBIGUOUS_OVERLAY
    return None, NotifyReason.NO_MESSAGING_BACKEND


def _preflight_result(
    audience: NotifyAudience,
    idempotency_key: str,
    *,
    kind: NotifyKind,
    text: str,
    requested_push: bool = False,
) -> NotifyOutcome | None:
    """Resolve the pre-delivery short-circuits — an outcome to stop, ``None`` to proceed.

    An INTERNAL audience is logged and terminally recorded (never DM'd, deny-by-
    default), and one the push/pull classifier routes to PULL is recorded there —
    both return a named not-sent outcome. ``None`` means the notification is
    owner-audience and pushed, so delivery proceeds.

    ``requested_push`` carries the caller's stated intent into the push/pull
    classifier — see :mod:`teatree.core.modelkit.dm_channel_policy`.
    """
    from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if audience == NotifyAudience.INTERNAL:
        logger.info("notify_user INTERNAL (log-only, not DM'd) key=%s: %s", idempotency_key, text[:120])
        BotPing.record_logged(idempotency_key, kind=kind.value, text=text, audience=audience.value)
        return blocked(NotifyReason.INTERNAL_AUDIENCE)
    if classify(audience=audience, idempotency_key=idempotency_key, requested_push=requested_push) is DmChannel.PULL:
        record_pulled(idempotency_key=idempotency_key, kind=kind, text=text, audience=audience)
        return blocked(NotifyReason.ROUTED_TO_PULL)
    return None


def _deliver_dm(
    backend: MessagingBackend,
    *,
    user_id: str,
    text: str,
    blocks: list[RawAPIDict] | None = None,
    thread_ts: str | Callable[[str], str] | None = None,
) -> tuple[str, str, str]:
    """Open a DM and post ``text``, returning ``(channel, ts, failure)``.

    ``thread_ts`` names an explicit thread, or a callback that resolves a
    ticket thread after the DM channel opens. Empty posts at the DM root.

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

    The text goes out through ``backend.post_message`` (``chat.postMessage``),
    which always returns ``{"ok": true, "ts": "…"}``.
    """
    # A preview (`t3 loops tick --loop followup --dry-run`) installs a suppressor and must
    # not DM the owner either — a run that "posts nothing" still pinging them is a lie. The
    # action name pairs with `teatree.loop.followup_dry_run._OWNER_DM_ACTION`, which classes
    # it owner-facing so it never counts as an authorship breach. Inert with no suppressor.
    suppressed = suppressed_egress_response(user_id, "owner_dm_post", EgressKind.POST)
    if suppressed is not None:
        return "dry-run", str(suppressed.get("ts") or "dry-run"), ""
    channel = ""
    try:
        channel = backend.open_dm(user_id)
        if not channel:
            return "", "", "open_dm returned an empty channel (Slack conversations.open ok:false)"
        if isinstance(thread_ts, str):
            thread = thread_ts
        elif thread_ts is None:
            thread = ""
        else:
            thread = thread_ts(channel)
        # Pass ``blocks`` only when a table is actually present — the common
        # text-only path stays a 3-arg call, so any backend (or test double)
        # that predates the ``blocks`` kwarg keeps working unchanged.
        if blocks is None:
            response = backend.post_message(channel=channel, text=text, thread_ts=thread)
        else:
            response = backend.post_message(channel=channel, text=text, thread_ts=thread, blocks=blocks)
    except Exception as exc:  # noqa: BLE001 — notify must never bubble up
        return channel, "", str(exc)

    posted_ts = str(response.get("ts", "")) if isinstance(response, dict) else ""
    response_ok = bool(response.get("ok")) if isinstance(response, dict) else False
    if not response_ok or not posted_ts:
        slack_error = str(response.get("error", "")) if isinstance(response, dict) else ""
        detail = f"Slack post failed: {slack_error}" if slack_error else "Slack post returned no message ts"
        return channel, posted_ts, detail

    return channel, posted_ts, ""


def _operator_proxy_gate(
    text: str, blocks: list[RawAPIDict] | None, idempotency_key: str, addressee: Addressee
) -> NotifyOutcome | None:
    payload = text if blocks is None else f"{text}\n{json.dumps(blocks, sort_keys=True, default=str)}"
    verdict = _route_through_send_proxy(payload, destination=addressee.user_id, is_self_dm=addressee.is_owner)
    if addressee.is_owner or (
        verdict is not None and verdict.allowed and not verdict.payload_redacted and not verdict.redaction_failed
    ):
        return None
    error = verdict.reason if verdict is not None else "send-proxy unavailable"
    if verdict is not None and verdict.payload_redacted:
        error = "send-proxy redacted an operator DM"
    finalize_failed(idempotency_key=idempotency_key, error=error)
    return blocked(NotifyReason.DELIVERY_FAILED, error=error)


def _route_through_send_proxy(text: str, *, destination: str, is_self_dm: bool) -> SendVerdict | None:
    """Only the owner's DM receives the send-proxy self-DM carve-out."""
    try:
        return route_send(
            SendRequest(
                channel=SendChannel.SLACK,
                destination=destination,
                payload=text,
                action="notify_user",
                is_self_dm=is_self_dm,
                strict_redaction=not is_self_dm,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — send-proxy audit is a side path; never break notify.
        logger.debug("notify_user send-proxy audit failed: %s", exc)


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
    """Prefix the DM with a kind marker for easy scan-reading on mobile; a question card is sent as rendered."""
    if kind is NotifyKind.QUESTION:
        return text
    prefix = {
        NotifyKind.ANSWER: ":speech_balloon: *answer*",
        NotifyKind.INFO: ":information_source: *info*",
    }[kind]
    return f"{prefix}\n{normalize_slack_message(text)}"


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
    from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: ORM import needs the app registry

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
            # stored audience is always one the owner reads — redeclare it so
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


__all__ = [
    "NotifyKind",
    "NotifyOptions",
    "NotifyOutcome",
    "NotifyReason",
    "drain_undelivered_notifies",
    "notify_user",
    "notify_user_outcome",
    "resolve_owner_dm_backend",
    "resolve_user_channel",
    "resolve_user_id",
]
