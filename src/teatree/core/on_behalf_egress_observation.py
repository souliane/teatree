"""Observe on-behalf Slack egress outcomes for previews and audit callers."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from teatree.core.egress_transport import EgressKind
from teatree.types import RawAPIDict


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
