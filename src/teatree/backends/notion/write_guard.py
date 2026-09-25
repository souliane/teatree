"""Where the Notion integration token may write: under a configured root, never under a denied one.

Notion's capabilities cover the whole integration, and its API cannot say which pages a customer sees,
so the boundary is configuration. A write is refused unless the target or one of its ancestors is a
write-allowed root, and a write-denied root anywhere on that chain always wins. A chain that cannot be
resolved refuses too, so an outage never widens what the token may touch.
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx

from teatree.backends.notion.errors import NotionError, NotionWriteRefusedError, normalize_object_id
from teatree.core.gates.notion_write_roots import notion_write_roots

type ParentOf = Callable[[str], str | None]
type Clock = Callable[[], float]

_ANCESTOR_LIFETIME_SECONDS = 300.0
_MAX_DEPTH = 64


def _canonical(object_id: str) -> str:
    return object_id.replace("-", "").lower()


@dataclass(frozen=True, slots=True)
class WriteScope:
    allowed: frozenset[str]
    denied: frozenset[str]

    @classmethod
    def of(cls, *, allowed: Iterable[str], denied: Iterable[str]) -> "WriteScope":
        return cls(
            allowed=frozenset(_canonical(normalize_object_id(root)) for root in allowed),
            denied=frozenset(_canonical(normalize_object_id(root)) for root in denied),
        )

    @classmethod
    def for_overlay(cls, overlay: str | None) -> "WriteScope":
        allowed, denied = notion_write_roots(overlay)
        return cls.of(allowed=allowed, denied=denied)


class WriteGuard:
    def __init__(self, *, parent_of: ParentOf, scope: Callable[[], WriteScope], clock: Clock = time.monotonic) -> None:
        self._parent_of = parent_of
        self._scope = scope
        self._clock = clock
        self._parents: dict[str, tuple[str | None, float]] = {}

    def check(self, target: str) -> None:
        scope = self._scope()
        lineage = self._lineage(target)
        denied = next((node for node in lineage if _canonical(node) in scope.denied), None)
        if denied is not None:
            msg = f"refusing to write {target}: it sits under the write-denied root {denied}"
            raise NotionWriteRefusedError(msg)
        if not scope.allowed.intersection(_canonical(node) for node in lineage):
            msg = f"refusing to write {target}: none of its ancestors is a write-allowed root"
            raise NotionWriteRefusedError(msg)

    def _lineage(self, target: str) -> list[str]:
        lineage = [target]
        while (parent := self._parent(lineage[-1], target)) is not None:
            if _canonical(parent) in {_canonical(node) for node in lineage}:
                msg = f"refusing to write {target}: its parent chain loops at {parent}"
                raise NotionWriteRefusedError(msg)
            if len(lineage) >= _MAX_DEPTH:
                msg = f"refusing to write {target}: its parent chain is deeper than {_MAX_DEPTH}"
                raise NotionWriteRefusedError(msg)
            lineage.append(parent)
        return lineage

    def _parent(self, node: str, target: str) -> str | None:
        now = self._clock()
        cached = self._parents.get(_canonical(node))
        if cached is not None and now - cached[1] < _ANCESTOR_LIFETIME_SECONDS:
            return cached[0]
        try:
            parent = self._parent_of(node)
        except (NotionError, httpx.HTTPError) as exc:
            msg = f"refusing to write {target}: the parent of {node} could not be read ({type(exc).__name__})"
            raise NotionWriteRefusedError(msg) from exc
        self._parents[_canonical(node)] = (parent, now)
        return parent
