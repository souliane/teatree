"""The label gate every issue-intake path runs before it creates work.

Two intakes turn an assigned issue into work: the ``assigned_issues`` scanner
(loop signals) and ``t3 <overlay> followup sync`` (``Ticket`` rows). Both answer
to the same overlay policy, so the predicate lives here rather than in either
caller — an intake that carries its own copy, or none at all, silently picks up
issues the operator never nominated.

An empty ``ready_labels`` admits everything, so an overlay that has not opted
into an allowlist keeps its pre-allowlist intake behaviour.
"""

from collections.abc import Iterable
from dataclasses import dataclass


def intake_admits(
    labels: Iterable[str],
    ready_labels: Iterable[str],
    exclude_labels: Iterable[str],
) -> bool:
    present = set(labels)
    allowlist = set(ready_labels)
    if allowlist and not present & allowlist:
        return False
    return not present & set(exclude_labels)


@dataclass(frozen=True, slots=True)
class LabelPolicy:
    """An overlay's allowlist/denylist as one value, for threading through intake layers.

    The scanner already holds the two lists as its own fields; the sync path
    crosses several call layers, where one policy argument beats two parallel
    ones that can drift apart.
    """

    ready_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()

    def admits(self, labels: Iterable[str]) -> bool:
        return intake_admits(labels, self.ready_labels, self.exclude_labels)
