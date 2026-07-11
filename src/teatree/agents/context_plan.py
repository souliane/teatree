"""Context-assembly + prompt-cache control API (#3157 E2).

A factory dispatches many short tasks against the same large repo context. Without a
cache-control surface, that repo-scale prefix (system context + conventions + repo digest)
is re-paid on every dispatch. Neither lane exposed one before: the Agent SDK owns caching
internally (core only observes cache tokens post-hoc), and the OpenAI-compatible router
binding deliberately sends no markers.

:class:`ContextPlan` is the small core type the option-builder assembles and a harness
consumes: ordered :class:`ContextSegment` segments tagged by :class:`SegmentStability`
(``static`` / ``per_repo`` / ``per_task`` / ``volatile``) with explicit cache boundaries.
The cacheable HEAD — the ``static`` + ``per_repo`` prefix up to the last breakpoint — must
be BYTE-STABLE across dispatches for the same repo (no timestamps/uuids), so provider-side
caching actually hits; :func:`find_unstable_tokens` / :func:`assert_byte_stable_head`
enforce that. The direct Anthropic Messages-API binding (#3157 E1b) maps each ``cache``
boundary to a real ``cache_control`` breakpoint with a TTL via :func:`cache_control_plan`;
the SDK lane can only honour segment ORDER (best effort).
"""

import re
from dataclasses import dataclass
from enum import IntEnum, StrEnum

#: Anthropic allows at most four ``cache_control`` breakpoints per request.
MAX_CACHE_BREAKPOINTS = 4

#: The two cache TTLs the Anthropic Messages API accepts.
CACHE_TTL_5M = "5m"
CACHE_TTL_1H = "1h"
_VALID_TTLS = frozenset({CACHE_TTL_5M, CACHE_TTL_1H})

# A timestamp or UUID in the cacheable head silently invalidates the cache on every
# dispatch — the head must be byte-stable. These patterns catch the usual invalidators:
# ISO-8601 timestamps, UUIDs, and epoch-like long digit runs.
_UNSTABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"),  # ISO datetime
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),  # UUID
    re.compile(r"\b\d{10,}\b"),  # epoch seconds / large volatile counters
)


class SegmentStability(StrEnum):
    """How often a context segment's content changes — its cache lifetime class.

    ``static`` is frozen across every repo and task (the teatree preamble, conventions);
    ``per_repo`` is frozen per repository (repo digest, schemas, overlay context);
    ``per_task`` changes per dispatch (task framing, parent-result bridge); ``volatile``
    changes every turn (timestamps, live diffs). Only ``static`` + ``per_repo`` may sit in
    the cacheable head — see :data:`CACHEABLE_STABILITIES`.
    """

    STATIC = "static"
    PER_REPO = "per_repo"
    PER_TASK = "per_task"
    VOLATILE = "volatile"


class _StabilityRank(IntEnum):
    STATIC = 0
    PER_REPO = 1
    PER_TASK = 2
    VOLATILE = 3


_RANK: dict[SegmentStability, _StabilityRank] = {
    SegmentStability.STATIC: _StabilityRank.STATIC,
    SegmentStability.PER_REPO: _StabilityRank.PER_REPO,
    SegmentStability.PER_TASK: _StabilityRank.PER_TASK,
    SegmentStability.VOLATILE: _StabilityRank.VOLATILE,
}

#: The stabilities eligible for the cacheable head (frozen for the life of a cache entry).
CACHEABLE_STABILITIES = frozenset({SegmentStability.STATIC, SegmentStability.PER_REPO})


@dataclass(frozen=True, slots=True)
class ContextSegment:
    """One ordered piece of assembled context, tagged with its cache lifetime.

    *cache* marks a cache breakpoint AFTER this segment — the direct Anthropic binding
    places a ``cache_control`` marker here. *ttl* is the cache lifetime (``5m`` / ``1h``).
    """

    content: str
    stability: SegmentStability
    cache: bool = False
    ttl: str = CACHE_TTL_5M

    def __post_init__(self) -> None:
        if self.ttl not in _VALID_TTLS:
            msg = f"Invalid cache TTL {self.ttl!r}; valid: {sorted(_VALID_TTLS)}"
            raise ValueError(msg)
        if self.cache and self.stability not in CACHEABLE_STABILITIES:
            msg = (
                f"A cache breakpoint may only sit on a {sorted(s.value for s in CACHEABLE_STABILITIES)} "
                f"segment, not a {self.stability.value!r} one — a breakpoint after volatile "
                "content would cache the volatile tail and never hit."
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CacheBreakpoint:
    """A resolved cache breakpoint: the segment index it sits after and its TTL."""

    segment_index: int
    ttl: str


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """An ordered, stability-tagged context assembly with explicit cache boundaries."""

    segments: tuple[ContextSegment, ...]

    @classmethod
    def ordered(cls, segments: "list[ContextSegment] | tuple[ContextSegment, ...]") -> "ContextPlan":
        """Build a plan with segments sorted by stability rank (stable within a rank).

        Sorting the ``static`` + ``per_repo`` prefix ahead of the per-task/volatile tail is
        what lets the cacheable head be byte-stable regardless of the caller's insertion
        order. The sort is stable, so segments of the same stability keep their relative
        order (a deterministic head).
        """
        ordered_segments = sorted(segments, key=lambda seg: _RANK[seg.stability])
        return cls(segments=tuple(ordered_segments))

    def render(self) -> str:
        """The full assembled context — every segment joined in order (the SDK-lane view)."""
        return "\n".join(seg.content for seg in self.segments)

    def cacheable_head(self) -> str:
        """The byte-stable prefix up to and including the last cache breakpoint.

        The content a long-TTL cache entry covers. Empty when the plan declares no cache
        breakpoint (nothing is pinned). Everything after the last breakpoint is the volatile
        tail, re-sent every dispatch.
        """
        last = self._last_cache_index()
        if last is None:
            return ""
        return "\n".join(seg.content for seg in self.segments[: last + 1])

    def cache_breakpoints(self) -> tuple[CacheBreakpoint, ...]:
        """The resolved cache breakpoints, capped at :data:`MAX_CACHE_BREAKPOINTS`.

        Anthropic accepts at most four; when more segments are marked ``cache`` the LAST
        four win (the deepest prefix is the most valuable to keep cached), so an
        over-marked plan degrades to the four-breakpoint contract instead of erroring.
        """
        marked = [i for i, seg in enumerate(self.segments) if seg.cache]
        kept = marked[-MAX_CACHE_BREAKPOINTS:]
        return tuple(CacheBreakpoint(segment_index=i, ttl=self.segments[i].ttl) for i in kept)

    def _last_cache_index(self) -> int | None:
        for i in range(len(self.segments) - 1, -1, -1):
            if self.segments[i].cache:
                return i
        return None


def find_unstable_tokens(text: str) -> list[str]:
    """Return every timestamp/uuid-like token in *text* that would invalidate a cache.

    The building block of the byte-stable-head guarantee: a cacheable head carrying any of
    these changes on every dispatch, so provider-side caching never hits. Returns the
    matched substrings (empty when the text is stable) so a test/lint can name the offender.
    """
    found: list[str] = []
    for pattern in _UNSTABLE_PATTERNS:
        found.extend(match.group(0) for match in pattern.finditer(text))
    return found


class UnstableCacheHeadError(ValueError):
    """The cacheable head carries a timestamp/uuid — it would invalidate the cache."""


def assert_byte_stable_head(plan: ContextPlan) -> None:
    """Raise :class:`UnstableCacheHeadError` when *plan*'s cacheable head is not byte-stable.

    The enforced form of the byte-stable-head guarantee (#3157 E2b): no timestamps, uuids,
    or volatile counters may appear before the last cache breakpoint. A plan with no
    breakpoint has an empty head and always passes.
    """
    unstable = find_unstable_tokens(plan.cacheable_head())
    if unstable:
        msg = f"Cacheable head carries volatile tokens that would break caching: {unstable}"
        raise UnstableCacheHeadError(msg)


def cache_control_plan(plan: ContextPlan) -> tuple[CacheBreakpoint, ...]:
    """Map *plan*'s cache boundaries to the ``cache_control`` breakpoints a direct API applies.

    The direct Anthropic Messages-API binding (#3157 E1b) consumes this to place real
    ``cache_control`` markers with a TTL. Thin alias of :meth:`ContextPlan.cache_breakpoints`
    so the harness names the intent (map to cache_control) at the call site.
    """
    return plan.cache_breakpoints()
