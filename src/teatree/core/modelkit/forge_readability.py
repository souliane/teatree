"""The vocabulary for "the forge could not be READ", kept distinct from "the forge said NO".

A live merge read has three outcomes, not two: the forge answered yes, the forge
answered no, or the forge could not be read at all. Collapsing the third into the
second is fail-CLOSED and must stay that way — but it also LIES to the operator.
A recorded ``merge_safe`` verdict on an UNCHANGED pull request was reported
``stale — re-review needed`` for the length of a GitHub 503, because an empty
head read compares unequal to every reviewed SHA; the same window reported
``checks failed`` on a PR whose checks nobody had managed to look at. Each costs
a full cold re-review of a tree nobody touched.

Both halves of the vocabulary live HERE, under ``core.modelkit``, because the two
layers that need them may not import each other: ``core.models.ReviewVerdict``
is a leaf beneath ``core.backend_protocols``, and ``backends.forge_merge_rpc``
sits above both.

Refusing on UNREADABLE is not optional. Every gate that refuses on
:data:`CHECKS_FAILED` must refuse on :data:`CHECKS_UNREADABLE` too, which is what
:data:`REFUSING_CHECK_VERDICTS` exists to make un-forgettable: teach the
consumers the set, and a producer that later starts emitting the new value cannot
open a gate that used to be shut. The change this module carries is the WORD an
operator reads, never the posture.
"""

from dataclasses import dataclass
from typing import Protocol

HEAD_SHA_UNREADABLE = "\x00_teatree_head_sha_unreadable\x00"
"""Sentinel head SHA — the forge declined to answer, so it named no head at all.

``fetch_live_head_sha`` returns this when the query itself failed (a non-zero
``gh`` rc, an unreadable GitLab MR payload), distinct from ``""`` (the forge DID
answer, with no usable oid in it). The NUL bytes make it un-collidable with any
real oid, exactly as they do for ``backend_protocols.CHANGED_PATHS_UNAVAILABLE``.

It must never reach a fail-closed caller as a truthy string: those refuse on a
FALSY sha, so :meth:`~teatree.core.merge.ci_rollup.CodeHostQuery.live_head_sha`
normalises the sentinel back to ``""`` and only ``live_head_read`` reveals it.
"""

CHECKS_FAILED = "failed"
"""A required check REPORTED a red verdict — the forge was read, and it said no."""

CHECKS_UNREADABLE = "unreadable"
"""The required-checks rollup could not be read — no verdict exists to report.

Refused identically to :data:`CHECKS_FAILED` at every merge gate; the difference
is only what the operator is told, and therefore whether they burn a re-review or
simply retry the read.
"""

REFUSING_CHECK_VERDICTS = frozenset({CHECKS_FAILED, CHECKS_UNREADABLE})
"""The check verdicts a merge gate must REFUSE — read this, never ``== "failed"``.

The ordering property that makes introducing :data:`CHECKS_UNREADABLE` safe: the
consumers learn this set while the producer still emits only
:data:`CHECKS_FAILED`, so the new value is unreachable and behaviour is
byte-identical. A producer changed first, against consumers still comparing
``== "failed"``, would let an unreadable forge merge — a strictly worse defect
than the mis-wording being fixed.
"""


def head_sha_unreadable(sha: str) -> bool:
    """True iff *sha* is a NON-ANSWER about the head rather than a head.

    Both the explicit :data:`HEAD_SHA_UNREADABLE` sentinel and a blank read
    qualify: a real pull request always has a head oid, so a forge that named
    none did not answer the question either way.
    """
    return sha == HEAD_SHA_UNREADABLE or not sha.strip()


@dataclass(frozen=True, slots=True)
class LiveHeadRead:
    """One live-head read: the SHA the forge named, and whether it named one at all.

    ``sha`` is always safe to hand to a fail-closed caller — it is ``""`` whenever
    ``unreadable`` is true, so the sentinel can never be mistaken for an oid.
    """

    sha: str
    unreadable: bool

    @classmethod
    def of(cls, raw: str) -> "LiveHeadRead":
        """Classify a raw ``fetch_live_head_sha`` result."""
        unreadable = head_sha_unreadable(raw)
        return cls(sha="" if unreadable else raw, unreadable=unreadable)


@dataclass(frozen=True, slots=True)
class LiveChecksRead:
    """One live CI read at ONE commit: the verdict, plus the evidence for it.

    The sibling of :class:`LiveHeadRead` for the checks question. ``status`` speaks the
    same green/pending/failed/:data:`CHECKS_UNREADABLE` vocabulary every gate already
    reads, so a reading can be handed straight to a caller that compares against
    :data:`REFUSING_CHECK_VERDICTS`. ``detail`` carries what was read (or why it could not
    be), because a refusal that names its evidence is the difference between an operator
    retrying a read and burning a re-review.
    """

    status: str
    detail: str = ""

    @classmethod
    def unreadable(cls, reason: str) -> "LiveChecksRead":
        return cls(status=CHECKS_UNREADABLE, detail=reason)

    @property
    def is_failed(self) -> bool:
        return self.status == CHECKS_FAILED

    @property
    def is_unreadable(self) -> bool:
        return self.status == CHECKS_UNREADABLE


class LiveChecksProbe(Protocol):
    """Reads CI at one commit. Injected, so no model ever owns the network call."""

    def __call__(self, *, slug: str, head_sha: str) -> LiveChecksRead: ...
