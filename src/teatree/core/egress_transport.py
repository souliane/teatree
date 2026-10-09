from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum

from teatree.types import RawAPIDict


class EgressKind(StrEnum):
    POST = "post"
    REACTION = "reaction"


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
