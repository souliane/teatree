"""Which open merge requests are ONE unit of work — a pure connected-components pass.

No I/O — no forge call, no Django, no clock — exactly like
:mod:`teatree.core.review.mr_triage`: titles in, groups out, so the batching
policy is a table under test and the caller owns every read.

Grouping is TRANSITIVE, never a single group key. Two merge requests sharing any
signal are one unit, so A/B on a ticket reference and B/C on a feature flag are
one group of three. A single-key grouper splits that trio and releases A while C
is still unfinished — the exact premature broadcast the batch gate exists to
prevent.

Extraction is deliberately NARROW in the other direction, because the two errors
are not symmetric: an over-grouped merge request waits on a sibling it has
nothing to do with, silently and forever, while an under-grouped one is merely
released on its own. Hence :func:`signals_for` takes ``generic_scopes`` — a scope
every housekeeping change shares says nothing about being one unit of work — and
hence the two bracket literals that are a placeholder and a scanner prefix rather
than feature flags. A merge request yielding no signal is its own group of one,
which satisfies the batch gate trivially.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum


class SignalKind(StrEnum):
    TICKET = "ticket"
    FLAG = "flag"
    SCOPE = "scope"


@dataclass(frozen=True, slots=True)
class GroupSignal:
    kind: SignalKind
    value: str


# Forge-agnostic supersets of the shapes a merge-request title actually carries:
# any host and any namespace depth, rather than one forge's own pinned prefixes.
_TICKET_URL_RE = re.compile(r"https?://[^\s()\[\]]+/-/(?:issues|work_items)/[1-9]\d*")
_TICKET_REF_RE = re.compile(r"(?<![\w./#-])[A-Za-z0-9][\w.-]*(?:/[A-Za-z0-9][\w.-]*)*#[1-9]\d*")
_FLAG_RE = re.compile(r"\[(?P<flag>[^\s\]]+)\]")
_SCOPE_RE = re.compile(r"[a-z][a-z0-9]*\((?P<scope>[^)]*)\)!?:\s")

# Bracket literals that are NOT feature flags: the placeholder an author leaves
# when a change ships behind none, and the prefix a security scanner stamps on
# every merge request it opens. Grouping on either fuses unrelated work.
_NON_FLAG_LITERALS = frozenset({"none", "aikido"})


def signals_for(title: str, *, generic_scopes: frozenset[str]) -> frozenset[GroupSignal]:
    return frozenset(_signals(title, frozenset(scope.casefold() for scope in generic_scopes)))


def group_members(items: Iterable[tuple[str, str]], *, generic_scopes: frozenset[str]) -> dict[str, frozenset[str]]:
    components = _DisjointSet()
    claimant_of: dict[GroupSignal, str] = {}
    for url, title in items:
        components.add(url)
        for signal in signals_for(title, generic_scopes=generic_scopes):
            components.union(url, claimant_of.setdefault(signal, url))

    urls = tuple(components)
    members: dict[str, set[str]] = defaultdict(set)
    for url in urls:
        members[components.root(url)].add(url)
    return {url: frozenset(members[components.root(url)]) for url in urls}


def _signals(title: str, generic_scopes: frozenset[str]) -> Iterator[GroupSignal]:
    for pattern in (_TICKET_URL_RE, _TICKET_REF_RE):
        for match in pattern.finditer(title):
            yield GroupSignal(kind=SignalKind.TICKET, value=match.group())
    for match in _FLAG_RE.finditer(title):
        flag = match.group("flag")
        if flag.casefold() not in _NON_FLAG_LITERALS:
            yield GroupSignal(kind=SignalKind.FLAG, value=flag)
    scope = _conventional_commit_scope(title)
    if scope and scope not in generic_scopes:
        yield GroupSignal(kind=SignalKind.SCOPE, value=scope)


def _conventional_commit_scope(title: str) -> str:
    match = _SCOPE_RE.match(title.lstrip())
    return match.group("scope").strip().casefold() if match else ""


class _DisjointSet:
    """Union-find over merge-request urls, path-halving on every :meth:`root`."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def __iter__(self) -> Iterator[str]:
        return iter(self._parent)

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def root(self, item: str) -> str:
        while (parent := self._parent[item]) != item:
            self._parent[item] = self._parent[parent]
            item = parent
        return item

    def union(self, left: str, right: str) -> None:
        self._parent[self.root(left)] = self.root(right)
