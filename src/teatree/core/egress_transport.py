from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from teatree.types import RawAPIDict


class EgressKind(StrEnum):
    POST = "post"
    REACTION = "reaction"
    UPDATE = "update"
    DELETE = "delete"


type EgressSuppressor = Callable[[str, str, EgressKind], RawAPIDict]

_EGRESS_SUPPRESSOR: ContextVar[EgressSuppressor | None] = ContextVar("egress_suppressor", default=None)


@contextmanager
def suppress_on_behalf_egress(suppressor: EgressSuppressor) -> Iterator[None]:
    token = _EGRESS_SUPPRESSOR.set(suppressor)
    try:
        yield
    finally:
        _EGRESS_SUPPRESSOR.reset(token)


def egress_suppressed() -> bool:
    return _EGRESS_SUPPRESSOR.get() is not None


def suppressed_egress_response(target: str, action: str, kind: EgressKind) -> RawAPIDict | None:
    suppressor = _EGRESS_SUPPRESSOR.get()
    return suppressor(target, action, kind) if suppressor is not None else None


def run_egress_transport(
    target: str,
    action: str,
    kind: EgressKind,
    publish: Callable[[], RawAPIDict],
) -> RawAPIDict:
    response = suppressed_egress_response(target, action, kind)
    return publish() if response is None else response


class EgressOutcome(StrEnum):
    POSTED = "posted"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class EgressDestination:
    """Where a Slack call landed — bundled so ``observe_egress`` stays under the arg-count cap."""

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


def observe_egress(
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
def observe_egress_errors(
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
