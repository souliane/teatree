"""Request-scoped memo for repeated identical reads — a foundation leaf, no teatree deps.

One HTTP request re-derives the same read many times: the dashboard's loop page
resolved the effective settings four times (each an ``importlib.metadata``
entry-point scan over every installed distribution) and re-read the preset /
schedule / loop tables three to four times apiece. None of those can change
mid-render, so the repeats are pure waste.

A memoized entry lives only inside an explicitly entered :func:`request_scope`,
so the CLI, the loop tick, and every test keep today's uncached semantics unless
they opt in. :func:`invalidate` drops the whole memo; the dashboard middleware
calls it after any non-SELECT statement, which is what makes a POST that mutates
then re-renders in the same request read its own writes.
"""

import inspect
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_MISS = object()

_ACTIVE: ContextVar["dict[Hashable, object] | None"] = ContextVar("teatree_request_cache", default=None)


@contextmanager
def request_scope() -> Iterator[None]:
    """Memoize :func:`cached_per_request` reads for the duration of the block."""
    token = _ACTIVE.set({})
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def invalidate() -> None:
    """Drop every memoized entry; a no-op outside a scope."""
    active = _ACTIVE.get()
    if active is not None:
        active.clear()


def cached_per_request[**P, R](read: Callable[P, R]) -> Callable[P, R]:
    """Memoize *read* on its arguments for the enclosing :func:`request_scope`.

    Outside a scope the call runs unchanged. An argument that cannot be hashed
    runs unchanged too, so a caller never has to know whether its arguments are
    memoizable.
    """
    identity = f"{getattr(read, '__module__', '')}.{getattr(read, '__qualname__', repr(read))}"
    signature = inspect.signature(read)

    @wraps(read)
    def memoized(*args: P.args, **kwargs: P.kwargs) -> R:
        active = _ACTIVE.get()
        if active is None:
            return read(*args, **kwargs)
        try:
            key = _key(identity, signature, *args, **kwargs)
        except TypeError:
            return read(*args, **kwargs)
        hit = active.get(key, _MISS)
        if hit is not _MISS:
            return hit  # ty: ignore[invalid-return-type]
        value = read(*args, **kwargs)
        active[key] = value
        return value

    return memoized


def _key(identity: str, signature: inspect.Signature, *args: object, **kwargs: object) -> Hashable:
    """A hashable identity for one call, normalising how the caller spelled it.

    ``read("a")``, ``read(key="a")`` and a ``read()`` that defaults to ``"a"`` are
    the same read, so binding against the signature keeps them on one entry.
    """
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    key = (identity, tuple(sorted(bound.arguments.items())))
    hash(key)  # an unhashable argument raises TypeError here, where the caller expects it
    return key
