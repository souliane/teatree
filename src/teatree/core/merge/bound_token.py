"""One merge's repository-bound forge credential."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar


class BoundTokenScopeError(RuntimeError):
    pass


_bound: ContextVar[tuple[str, str] | None] = ContextVar("bound_merge_token", default=None)


@contextmanager
def bound_merge_token(slug: str, token: str) -> Generator[None]:
    marker = _bound.set((slug.casefold(), token))
    try:
        yield
    finally:
        _bound.reset(marker)


def bound_token_for(slug: str) -> str:
    current = _bound.get()
    if current is None:
        return ""
    if current[0] != slug.casefold():
        msg = f"bound merge token is for {current[0]}, not {slug.casefold()}"
        raise BoundTokenScopeError(msg)
    return current[1]
