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
    A wire call whose artifact did NOT land (``ok:false`` other than
    ``already_reacted``, or an empty body) rolls the gate's approval consume
    and audit back, so the single-use approval survives for a retry.

It reuses the three existing seams verbatim — the pre-gate
(:mod:`teatree.core.on_behalf_gate_recorded`), the after-receipt audit
(:mod:`teatree.core.on_behalf_post_receipt`), and the backend's #1750
``route_token`` / ``post_routed`` / ``react_routed`` — adding no new gate
logic, no new audit ledger, no new model/setting/protocol.

The methods return the raw Slack body so callers keep their existing
``ok`` / ``error`` / ``already_reacted`` / ``missing_scope`` mapping;
transport exceptions propagate to each caller's existing ``try``/``except``.

Scope is *only* colleague Slack post/react. It does not own bot→user DM
sinks (``notify_user``, ``reply_transport.post_dm``, ``speak``) — already
correct and ungated by design — the FSM
``signals.py`` reactions (already gate+audit-correct on the separate
``slack_reactions`` single-bot-token transport), or the GitLab
approve/comment paths (already gated via ``check_on_behalf``).

Home is :mod:`teatree.core`: both the gate and the audit already live
here, and ``MessagingBackend``/``RawAPIDict`` are owned by ``teatree.core``
/``teatree.types`` — no edge into ``teatree.backends``.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.egress_transport import EgressKind, run_egress_transport, suppress_on_behalf_egress
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.send_proxy import SendBlockedError, SendChannel, SendRequest, route_send
from teatree.on_behalf_gate import OnBehalfContext
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


class EgressOutcome(StrEnum):
    POSTED = "posted"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class EgressDestination:
    """Where a Slack call landed — bundled so ``_observe_egress`` stays under the arg-count cap."""

    channel: str = ""
    thread: str = ""


_NO_DESTINATION = EgressDestination()


@dataclass(frozen=True, slots=True)
class EgressAttempt:
    target: str
    action: str
    kind: EgressKind
    outcome: EgressOutcome
    reason: str
    channel: str = ""
    thread: str = ""


type EgressObserver = Callable[[EgressAttempt], None]

_EGRESS_OBSERVER: ContextVar[EgressObserver | None] = ContextVar("on_behalf_egress_observer", default=None)


@contextmanager
def observe_on_behalf_egress(observer: EgressObserver) -> Iterator[None]:
    token = _EGRESS_OBSERVER.set(observer)
    try:
        yield
    finally:
        _EGRESS_OBSERVER.reset(token)


def _observe_egress(
    target: str,
    action: str,
    kind: EgressKind,
    response: RawAPIDict,
    *,
    destination: EgressDestination = _NO_DESTINATION,
) -> RawAPIDict:
    landed = bool(response.get("ok")) or response.get("error") == "already_reacted"
    if observer := _EGRESS_OBSERVER.get():
        reason = (
            "all live checks passed; final transport suppressed"
            if landed
            else str(response.get("error") or "no response")
        )
        observer(
            EgressAttempt(
                target=target,
                action=action,
                kind=kind,
                outcome=EgressOutcome.POSTED if landed else EgressOutcome.REFUSED,
                reason=reason,
                channel=destination.channel,
                thread=destination.thread,
            ),
        )
    return response


@contextmanager
def _observe_egress_errors(
    target: str,
    action: str,
    kind: EgressKind,
    *,
    destination: EgressDestination = _NO_DESTINATION,
) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        if observer := _EGRESS_OBSERVER.get():
            observer(
                EgressAttempt(
                    target=target,
                    action=action,
                    kind=kind,
                    outcome=EgressOutcome.REFUSED,
                    reason=" ".join(str(exc).splitlines()),
                    channel=destination.channel,
                    thread=destination.thread,
                ),
            )
        raise


def routed_channel_text(*, target: str, action: str, channel: str, text: str) -> str:
    """The text a gated caller may post to *channel*, send-proxied ahead of its approval gate.

    Outside the gate's transaction on purpose: a refusal raised inside it rolls the ``SendAudit`` row back.
    """
    with _observe_egress_errors(target, action, EgressKind.POST, destination=EgressDestination(channel=channel)):
        return _route_colleague_send(channel=channel, payload=text, action=action, target=target)


def observed_channel_post(*, target: str, action: str, channel: str, publish: Callable[[], RawAPIDict]) -> RawAPIDict:
    """A gated caller's own channel post, suppressed and reported by a preview exactly as this egress's are."""
    destination = EgressDestination(channel=channel)
    with _observe_egress_errors(target, action, EgressKind.POST, destination=destination):
        response = run_egress_transport(target, action, EgressKind.POST, publish)
    return _observe_egress(target, action, EgressKind.POST, response, destination=destination)


class _PublishDidNotLandError(Exception):
    """Carries the raw body of a wire call that did not put the artifact on the surface.

    Raised INSIDE the gate's ``publish`` callback so its ``transaction.atomic``
    rolls the approval consume and the audit back (#1879) — a Slack ``ok:false``
    must not spend a single-use approval or record a post that never landed.
    """

    def __init__(self, response: RawAPIDict) -> None:
        super().__init__(str(response.get("error") or "no response"))
        self.response = response


#: What the egress reports when the wire call never ran: the backend resolved no token for
#: the destination — an overlay with no Slack transport, or no user-OAuth token for a
#: colleague surface. Named here once, so no caller prints it as ``unknown_error``.
NO_TOKEN_FOR_DESTINATION: RawAPIDict = {"ok": False, "error": "no_token_for_destination"}


def _publish_or_rollback(publish: Callable[[], RawAPIDict]) -> RawAPIDict:
    """Run *publish* and raise :class:`_PublishDidNotLandError` unless the artifact landed.

    ``already_reacted`` IS landed — the reaction is present, the call was just the
    idempotent no-op. An empty body (no token resolved) is not, and is reported as
    :data:`NO_TOKEN_FOR_DESTINATION`.
    """
    response = publish()
    if response.get("ok") or response.get("error") == "already_reacted":
        return response
    raise _PublishDidNotLandError(response or dict(NO_TOKEN_FOR_DESTINATION))


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
        # Resolved by name, like the ``route_token`` probe above: ``_is_self_dm`` is a
        # backend-private helper the MessagingBackend protocol does not declare.
        is_self_dm = getattr(self._messaging, "_is_self_dm", None)
        return bool(is_self_dm(channel)) if callable(is_self_dm) else False

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
        context: OnBehalfContext | None = None,
    ) -> RawAPIDict:
        """React on *channel*'s message, gated+audited on a colleague surface.

        Self-DM: react raw via ``react_routed`` and return (ungated,
        unaudited). Colleague/channel: gate first (raises
        :class:`OnBehalfPostBlockedError` on BLOCK with no recorded
        approval, before any wire call), react, then DM the after-receipt
        notice only when the reaction *really* landed (``ok`` truthy — never
        on ``already_reacted`` or ``ok:false``). Returns the raw Slack body.
        """
        egress_location = EgressDestination(channel=channel, thread=ts)
        with _observe_egress_errors(target, action, EgressKind.REACTION, destination=egress_location):
            if self._is_self_dm(channel):
                response = run_egress_transport(
                    target,
                    action,
                    EgressKind.REACTION,
                    lambda: self._messaging.react_routed(channel=channel, ts=ts, emoji=emoji),
                )
                return _observe_egress(target, action, EgressKind.REACTION, response, destination=egress_location)
            _route_colleague_send(channel=channel, payload=f":{emoji}:", action=action, target=target)
            try:
                response = require_on_behalf_approval(
                    target=target,
                    action=action,
                    context=context,
                    publish=lambda: _publish_or_rollback(
                        lambda: run_egress_transport(
                            target,
                            action,
                            EgressKind.REACTION,
                            lambda: self._messaging.react_routed(channel=channel, ts=ts, emoji=emoji),
                        ),
                    ),
                )
            except _PublishDidNotLandError as unlanded:
                return _observe_egress(
                    target,
                    action,
                    EgressKind.REACTION,
                    unlanded.response,
                    destination=egress_location,
                )
            if response.get("ok"):
                notify_user_on_behalf_post(
                    target=target,
                    action=action,
                    destination=destination or channel,
                    artifact_url=artifact_url or channel,
                    summary=summary or f":{emoji}:",
                )
            return _observe_egress(target, action, EgressKind.REACTION, response, destination=egress_location)

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
        context: OnBehalfContext | None = None,
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
        under an open question never reaches here, and a DM Slack refused
        (``ok:false``, or no ``ts``) leaves the question pending for the next
        answerer pass rather than retiring an answer nobody received.
        Colleague/channel: gate
        first (raises :class:`OnBehalfPostBlockedError` on BLOCK with no
        recorded approval, before any wire call), post, then DM the
        after-receipt notice only on a successful publish (``ok`` truthy) —
        a colleague surface is never read aloud. Returns the raw Slack body
        so callers keep inspecting ``ok`` / ``error`` / ``ts``.
        """
        egress_location = EgressDestination(channel=channel, thread=thread_ts)
        with _observe_egress_errors(target, action, EgressKind.POST, destination=egress_location):
            if self._is_self_dm(channel):

                def publish_self_dm() -> RawAPIDict:
                    from teatree.core.speak import (  # noqa: PLC0415 — deferred: call-time import, kept lazy
                        deliver_user_dm,
                    )

                    return deliver_user_dm(self._messaging, channel=channel, text=text, thread_ts=thread_ts)

                response = run_egress_transport(target, action, EgressKind.POST, publish_self_dm)
                if response.get("ok") and response.get("ts"):
                    _retire_threaded_answer(thread_ts)
                return _observe_egress(target, action, EgressKind.POST, response, destination=egress_location)
            text = _route_colleague_send(channel=channel, payload=text, action=action, target=target)
            try:
                response = require_on_behalf_approval(
                    target=target,
                    action=action,
                    context=context,
                    publish=lambda: _publish_or_rollback(
                        lambda: run_egress_transport(
                            target,
                            action,
                            EgressKind.POST,
                            lambda: self._messaging.post_routed(channel=channel, text=text, thread_ts=thread_ts),
                        ),
                    ),
                )
            except _PublishDidNotLandError as unlanded:
                return _observe_egress(target, action, EgressKind.POST, unlanded.response, destination=egress_location)
            if response.get("ok"):
                notify_user_on_behalf_post(
                    target=target,
                    action=action,
                    destination=destination or channel,
                    artifact_url=channel,
                    summary=summary or text[:120],
                )
            return _observe_egress(target, action, EgressKind.POST, response, destination=egress_location)


def _route_colleague_send(*, channel: str, payload: str, action: str, target: str) -> str:
    """Route a colleague-surface Slack send through the #117 send-proxy.

    Returns the possibly redacted payload to post. Raises
    :class:`~teatree.core.send_proxy.SendBlockedError` when the destination is
    absent from the allowlist — a pre-wire block that composes with the
    on-behalf gate below it.
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
    :class:`PendingChatInjection` row is stamped on BOTH columns in one CAS —
    ``loop_replied_at`` so the reactive cycle stops re-delegating a
    ``t3:answerer`` Task, and ``answered_at`` (the agent personally replied).
    Best-effort: a top-level self-DM (no ``thread_ts``) is a no-op and any DB
    failure is logged and swallowed so the DM is never lost.
    """
    if not thread_ts:
        return
    from teatree.core.models import PendingChatInjection  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        PendingChatInjection.retire_answered_in_thread(thread_ts)
    except Exception as exc:  # noqa: BLE001 — retiring is a side path; never drop the DM
        logger.debug("retire-answered-question stamp failed for thread_ts=%s: %s", thread_ts, exc)


__all__ = [
    "NO_TOKEN_FOR_DESTINATION",
    "EgressAttempt",
    "EgressKind",
    "EgressOutcome",
    "OnBehalfPostBlockedError",
    "OnBehalfSlackEgress",
    "observe_on_behalf_egress",
    "observed_channel_post",
    "routed_channel_text",
    "suppress_on_behalf_egress",
]
