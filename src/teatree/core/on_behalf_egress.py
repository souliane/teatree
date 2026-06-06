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
    when ``slack_audio`` is on and plays it locally when ``local`` is on
    (#2050), so the ``notify post`` user-DM path runs the same DM+speak
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

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.types import RawAPIDict


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
        ``D…`` id can never be mistaken for self. Fail-closed: a backend
        with no ``route_token`` accessor (a fake / Noop with no #1750
        router) is treated as a colleague surface — an unclassifiable
        destination fires the gate rather than slipping past it.
        """
        if getattr(self._messaging, "route_token", None) is None:
            return False
        return bool(self._messaging._is_self_dm(channel))  # type: ignore[attr-defined]  # noqa: SLF001

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
        "text DM to the user" #2050 targets, so the user's own DM both
        attaches spoken audio when ``slack_audio`` is on AND plays locally
        when ``local`` is on, driven by the SAME chokepoint
        :func:`teatree.core.notify.notify_user` uses (one place owns the
        speak logic for both DM egress points). Colleague/channel: gate
        first (raises :class:`OnBehalfPostBlockedError` on BLOCK with no
        recorded approval, before any wire call), post, then DM the
        after-receipt notice only on a successful publish (``ok`` truthy) —
        a colleague surface is never read aloud. Returns the raw Slack body
        so callers keep inspecting ``ok`` / ``error`` / ``ts``.
        """
        if self._is_self_dm(channel):
            from teatree.core.speak import deliver_user_dm  # noqa: PLC0415

            return deliver_user_dm(self._messaging, channel=channel, text=text, thread_ts=thread_ts)
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


__all__ = ["OnBehalfPostBlockedError", "OnBehalfSlackEgress"]
