"""One merge's repository-bound forge credential."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING

from teatree import forge_credentials
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.core.merge.ci_rollup import CodeHostQuery


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


def bound_token_active(slug: str) -> bool:
    current = _bound.get()
    return current is not None and current[0] == slug.casefold()


def bound_token_for(slug: str) -> str:
    current = _bound.get()
    if current is None:
        return ""
    if current[0] != slug.casefold():
        msg = f"bound merge token is for {current[0]}, not {slug.casefold()}"
        raise BoundTokenScopeError(msg)
    return current[1]


def bind_repository_merge_token(merge: Callable[..., str]) -> Callable[..., str]:
    @wraps(merge)
    def bound(
        *,
        ref: PrRef,
        expected_head_oid: str,
        squash: bool = True,
        query: "CodeHostQuery | None" = None,
    ) -> str:
        if ref.host_kind == "gitlab" and not bound_token_active(ref.slug):
            resolution = forge_credentials.resolve_slug_token(ref.slug, forge="gitlab", credential="gitlab_token")
            if resolution.state is forge_credentials.ForgeTokenState.TOKEN:
                with bound_merge_token(ref.slug, resolution.token):
                    return merge(ref=ref, expected_head_oid=expected_head_oid, squash=squash, query=query)
        return merge(ref=ref, expected_head_oid=expected_head_oid, squash=squash, query=query)

    return bound
