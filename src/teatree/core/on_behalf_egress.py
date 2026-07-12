"""The single cohesive owner of colleague-surface Slack on-behalf egress (#960/#1750).

Every Slack post or reaction made *under the user's identity on a
colleague surface* goes through :class:`OnBehalfSlackEgress`. One instance
wraps one :class:`~teatree.core.backend_protocols.MessagingBackend`; each
public method runs the identical fixed sequence in one place:

1.  classify the destination self-vs-colleague via the backend's #1750
    ``route_token`` classifier;
2.  a *self* destination (the user's own DM) → deliver via the shared
    :func:`teatree.core.speak.deliver_user_dm` chokepoint and return —
    ungated and unaudited, because a bot→user / self-ack is never an
    on-behalf post. ``deliver_user_dm`` attaches spoken audio to the DM
    when ``slack`` is on and plays it locally when ``local`` plays DMs
    (#2060), so the ``notify post`` user-DM path runs the same DM+speak
    chokepoint :func:`teatree.core.notify.notify_user` does;
3.  a *colleague/channel* destination → :func:`require_on_behalf_approval`
    *before* the wire call (a BLOCK verdict with no recorded approval
    raises :class:`OnBehalfPostBlockedError` and nothing posts), then the
    routed wire call (``post_routed`` / ``react_routed``), then
    :func:`notify_user_on_behalf_post` — but only on a *real* successful
    publish (``ok`` truthy), never on ``already_reacted`` or ``ok:false``.

It reuses the three existing seams verbatim — the pre-gate
(:mod:`teatree.core.on_behalf_gate_recorded`), the after-receipt audit
(:mod:`teatree.core.on_behalf_post_receipt`), and the backend's #1750
``route_token`` / ``post_routed`` / ``react_routed`` — adding no new gate
logic, no new audit ledger, no new model/setting/protocol.

The methods return the raw Slack body so callers keep their existing
``ok`` / ``error`` / ``already_reacted`` / ``missing_scope`` mapping;
transport exceptions propagate to each caller's existing ``try``/``except``.

Scope is *only* colleague Slack post/react. It does not own bot→user DM
sinks (``notify_user``, ``reply_transport.post_dm``, the daily digest,
``speak``) — already correct and ungated by design — the FSM
``signals.py`` reactions (already gate+audit-correct on the separate
``slack_reactions`` single-bot-token transport), or the GitLab
approve/comment paths (already gated via ``check_on_behalf``).

Home is :mod:`teatree.core`: both the gate and the audit already live
here, and ``MessagingBackend``/``RawAPIDict`` are owned by ``teatree.core``
/``teatree.types`` — no edge into ``teatree.backends``.
"""

import logging

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.send_proxy import SendBlockedError, SendChannel, SendRequest, route_send
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


class OnBehalfSlackEgress:
    """Gate→route→emit→audit for one colleague-surface Slack post/react.

    Constructed inline from the ``MessagingBackend`` each call site already
    holds — no singleton, no factory, no DI container, same lifetime as the
    backend it wraps.
    """

    def __init__(self, messaging: MessagingBackend) -> None:
        self._messaging = messaging

    def _is_self_dm(self, channel: str) -> bool:
        """True when *channel* is the user's own DM, via the backend's #1750 classifier.

        Reuses the single #1750 destination test rather than inventing a
        second classifier: the on-behalf carve-out boundary and the
        token-routing boundary are the same line of truth, so a colleague's
        ``D…`` id can never be mistaken for self. Fail-closed: unknown
        surface (no ``route_token`` accessor) is treated as colleague surface
        with explicit logging of the surface name to aid debugging.
        """
        if getattr(self._messaging, "route_token", None) is None:
            logger.warning("unclassifiable surface (no route_token): %s — treating as colleague surface", channel)
            return False
        return bool(self._messaging._is_self_dm(channel))  # type: ignore[attr-defined]  # noqa: SLF001

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def react(  # noqa: PLR0913 — colleague-egress chokepoint; each kwarg is a documented gate/route/audit input, kwargs-only.
        self,
        *,
        channel: str,
        ts: str,
        emoji: str,
        target: str,
        action: str,
        destination: str = "",
        artifact_url: str = "",
        summary: str = "",
    ) -> RawAPIDict:
        """React on *channel*'s message, gated+audited on a colleague surface.

        Self-DM: react raw via ``react_routed`` and return (ungated,
        unaudited). Colleague/channel: gate first (raises
        :class:`OnBehalfPostBlockedError` on BLOCK with no recorded
        approval, before any wire call), react, then DM the after-receipt
        notice only when the reaction *really* landed (``ok`` truthy — never
        on ``already_reacted`` or ``ok:false``). Returns the raw Slack body.
        """
        if self._is_self_dm(channel):
            return self._messaging.react_routed(channel=channel, ts=ts, emoji=emoji)
        _route_colleague_send(channel=channel, payload=f":{emoji}:", action=action, target=target)
        response = require_on_behalf_approval(
            target=target,
            action=action,
            publish=lambda: self._messaging.react_routed(channel=channel, ts=ts, emoji=emoji),
        )
        if response.get("ok"):
            notify_user_on_behalf_post(
                target=target,
                action=action,
                destination=destination or channel,
                artifact_url=artifact_url or channel,
                summary=summary or f":{emoji}:",
            )
        return response

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def post(  # noqa: PLR0913 — colleague-egress chokepoint; each kwarg is a documented gate/route/audit input, kwargs-only.
        self,
        *,
        channel: str,
        text: str,
        target: str,
        action: str,
        thread_ts: str = "",
        destination: str = "",
        summary: str = "",
    ) -> RawAPIDict:
        """Post to *channel*, gated+audited on a colleague surface.

        Self-DM: deliver via the shared :func:`teatree.core.speak.deliver_user_dm`
        chokepoint (ungated, unaudited) — a bot→user DM is exactly the
        "text DM to the user" #2060 targets, so the user's own DM both
        attaches spoken audio when ``slack`` is on AND plays locally
        when ``local`` plays DMs, driven by the SAME chokepoint
        :func:`teatree.core.notify.notify_user` uses (one place owns the
        speak logic for both DM egress points). When this self-DM is a
        DELIBERATE threaded reply (``thread_ts`` set — the ``notify post
        --thread-ts`` answer route), it retires the queued question that
        thread roots on (#2053): only this answer path deliberately threads
        under the question, so the retire fires iff the DM is genuinely an
        answer — an unrelated INFO DM that ``notify_user`` happens to thread
        under an open question never reaches here. Colleague/channel: gate
        first (raises :class:`OnBehalfPostBlockedError` on BLOCK with no
        recorded approval, before any wire call), post, then DM the
        after-receipt notice only on a successful publish (``ok`` truthy) —
        a colleague surface is never read aloud. Returns the raw Slack body
        so callers keep inspecting ``ok`` / ``error`` / ``ts``.
        """
        if self._is_self_dm(channel):
            from teatree.core.speak import deliver_user_dm  # noqa: PLC0415 — deferred: call-time import, kept lazy

            response = deliver_user_dm(self._messaging, channel=channel, text=text, thread_ts=thread_ts)
            _retire_threaded_answer(thread_ts)
            return response
        text = _route_colleague_send(channel=channel, payload=text, action=action, target=target)
        response = require_on_behalf_approval(
            target=target,
            action=action,
            publish=lambda: self._messaging.post_routed(channel=channel, text=text, thread_ts=thread_ts),
        )
        if response.get("ok"):
            notify_user_on_behalf_post(
                target=target,
                action=action,
                destination=destination or channel,
                artifact_url=channel,
                summary=summary or text[:120],
            )
        return response


def _route_colleague_send(*, channel: str, payload: str, action: str, target: str) -> str:
    """Route a colleague-surface Slack send through the #117 send-proxy.

    Returns the (possibly redacted, in ``enforce`` mode) payload to post. Raises
    :class:`~teatree.core.send_proxy.SendBlockedError` when the proxy refuses the
    destination (``enforce`` mode, destination absent from the allowlist) — a
    pre-wire block that composes with the on-behalf gate below it. On the ``warn``
    ship default the proxy always allows and returns the payload unchanged, so
    this is an audit-only pass.
    """
    verdict = route_send(
        SendRequest(
            channel=SendChannel.SLACK,
            destination=channel,
            payload=payload,
            action=action,
            target=target,
        ),
    )
    if not verdict.allowed:
        raise SendBlockedError(verdict)
    return verdict.payload


def _retire_threaded_answer(thread_ts: str) -> None:
    """Retire the queued question a deliberate threaded self-DM answer replies to (#2053).

    Called only from the self-DM branch of :meth:`OnBehalfSlackEgress.post`,
    which is the ``notify post --thread-ts`` answer egress: the caller
    deliberately threads the reply under the question, so ``thread_ts`` here
    is a genuine "this DM answers that question" signal (unlike the shared
    :func:`teatree.core.speak.deliver_user_dm` chokepoint, which carries the
    most-recent active DM thread for any INFO/status DM). The matching
    :class:`PendingChatInjection` row is stamped on BOTH gates in one CAS —
    ``loop_replied_at`` so the reactive cycle stops re-delegating a
    ``t3:answerer`` Task, and ``answered_at`` so the #1063 Stop-hook gate
    stops nagging. Best-effort: a top-level self-DM (no ``thread_ts``) is a
    no-op and any DB failure is logged and swallowed so the DM is never lost.
    """
    if not thread_ts:
        return
    from teatree.core.models import PendingChatInjection  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        PendingChatInjection.retire_answered_in_thread(thread_ts)
    except Exception as exc:  # noqa: BLE001 — retiring is a side path; never drop the DM
        logger.debug("retire-answered-question stamp failed for thread_ts=%s: %s", thread_ts, exc)


__all__ = ["OnBehalfPostBlockedError", "OnBehalfSlackEgress"]
