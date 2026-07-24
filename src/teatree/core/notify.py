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

:func:`notify_user_outcome` is the egress; it returns a :class:`NotifyOutcome`
naming the :class:`NotifyReason` for every non-delivery, because a silent
notification failure is worse than a loud one — nobody is watching the CLI turn,
which is the entire premise of this module, so an unexplained ``False`` reaches
no one. :func:`notify_user` is the boolean face of it for call sites that only
branch on delivered-or-not. Both are no-op-safe and never raise into the CLI turn.
"""

import logging
import os

from django.db import DatabaseError

from teatree.config import get_effective_settings
from teatree.core.backend_factory import OwnerMessagingTransport, messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify_ledger import (
    already_sent_noop,
    claim_delivery_slot,
    finalize_failed,
    finalize_sent,
    maybe_stamp_answered,
    record_noop,
    record_outbound_claim,
)
from teatree.core.notify_targets import resolve_user_channel, resolve_user_id
from teatree.core.notify_types import (
    DEFAULT_NOTIFY_OPTIONS,
    DELIVERED,
    NotifyKind,
    NotifyOptions,
    NotifyOutcome,
    NotifyReason,
    blocked,
)
from teatree.core.send_proxy import SendChannel, SendRequest, route_send
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
    ``answered_at`` stamped so the Stop hook stops nagging. Alternatively
    use an idempotency-key of the form ``answer-<anything>-<slack_ts>``
    and the same auto-stamp triggers — useful for callers that don't
    plumb the explicit kwarg through.
    """
    kind_value = NotifyKind(kind) if not isinstance(kind, NotifyKind) else kind

    early = _preflight_result(audience, idempotency_key, kind=kind_value, text=text)
    if early is not None:
        return early

    already = already_sent_noop(idempotency_key)
    if already is not None:
        return already

    resolved_backend, backend_refusal = (
        (options.backend, NotifyReason.NONE) if options.backend is not None else resolve_owner_dm_backend()
    )
    resolved_user_id = options.user_id if options.user_id is not None else resolve_user_id()

    # Split, never conflated: the halves have different fixes, and a joint
    # message is what let a green "owner id resolves" doctor line coexist with a
    # dead transport for a full day. The backend half carries its own typed
    # refusal (none-at-all vs several-pick-one) from :func:`resolve_owner_dm_backend`.
    if resolved_backend is None or not resolved_user_id:
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

    _route_through_send_proxy(text, destination=resolved_user_id)

    payload_text = maybe_linkify(text) if options.linkify else text

    channel, posted_ts, failure = _deliver_dm(
        resolved_backend,
        user_id=resolved_user_id,
        text=format_notification(payload_text, kind_value),
        blocks=options.blocks,
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
    # ``channel`` and ``posted_ts`` are both non-empty here — ``_deliver_dm``
    # only returns no failure when both are set, so no defensive re-check.
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
) -> NotifyOutcome | None:
    """Resolve the pre-delivery short-circuits — an outcome to stop, ``None`` to proceed.

    An INTERNAL audience is logged and terminally recorded (never DM'd, deny-by-
    default), and a settings-disabled feature is skipped — both return a named
    not-sent outcome. ``None`` means the notification is owner-audience and
    enabled, so delivery proceeds.
    """
    from teatree.core.models import BotPing  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if audience == NotifyAudience.INTERNAL:
        logger.info("notify_user INTERNAL (log-only, not DM'd) key=%s: %s", idempotency_key, text[:120])
        BotPing.record_logged(idempotency_key, kind=kind.value, text=text, audience=audience.value)
        return blocked(NotifyReason.INTERNAL_AUDIENCE)
    if not _feature_enabled():
        logger.debug("notify_user disabled by settings — %s skipped", idempotency_key)
        return blocked(NotifyReason.FEATURE_DISABLED)
    return None


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
    except Exception as exc:  # noqa: BLE001 — notify must never bubble up
        return "", "", str(exc)

    posted_ts = str(response.get("ts", "")) if isinstance(response, dict) else ""
    response_ok = bool(response.get("ok")) if isinstance(response, dict) else False
    if not response_ok or not posted_ts:
        slack_error = str(response.get("error", "")) if isinstance(response, dict) else ""
        detail = f"Slack post failed: {slack_error}" if slack_error else "Slack post returned no message ts"
        return channel, posted_ts, detail

    # Delivery is CONFIRMED (``ok:true`` + a real ``ts``): only NOW run the
    # speak side-effects, threading the audio under the just-delivered message
    # (``thread_ts=posted_ts``) with an EMPTY ``initial_comment`` so the text
    # lands exactly ONCE (F4.4). Firing the sidecar before this check
    # double-delivered the text — the ``post_message`` body plus the audio DM's
    # identical ``initial_comment`` — and, on an ``ok:false`` post, attached
    # audio to a DM that never landed; the FAILED finalize then drove a retry
    # that re-attached, tripling the audio. The sidecar is best-effort and must
    # never undo a DM that has already landed.
    try:
        deliver_user_dm_sidecar(backend, channel=channel, text=text, thread_ts=posted_ts, initial_comment="")
    except Exception as exc:  # noqa: BLE001 — sidecar is best-effort; a delivered DM must never be undone by it
        logger.debug("notify_user speak sidecar failed for key on channel=%s: %s", channel, exc)
    return channel, posted_ts, ""


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
