"""How much a memory is worth keeping HOT, and how many must go to fit the budget.

Split out of :mod:`teatree.loops.dream.decay`, which owns the archival POLICY and
mechanics; this owns the arithmetic those decisions rest on — the additive keep-HOT
score, and the projection of what the re-index will write once a given survivor set is
all that is left.

The projection is the part that has to be exact rather than approximately right. It is
the stop condition for the budget tier, so an over-estimate archives memories that
would have fitted and an under-estimate archives none at all and leaves gate (d)
failing with nothing naming the cause. It is therefore rendered through the SAME
:mod:`teatree.loops.dream.reindex` helpers the writer uses, preamble included.

DB-free and deterministic: every input is passed in, so this is usable under
``SimpleTestCase``.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from teatree.loops.dream._shared import is_binding_text
from teatree.loops.dream.decay_corpus import MemoryFile

#: Frontmatter ``type:`` (top-level or nested under ``metadata:``) — the memory's
#: declared kind, used for the type-weight signal. ``node_type:`` never matches.
_TYPE_LINE_RE = re.compile(r"^\s*type:\s*(\S+)\s*$", re.MULTILINE)

#: The recognised memory types (filename prefix or frontmatter ``metadata.type``).
_KNOWN_TYPES = frozenset({"user", "feedback", "retro", "reference", "project"})

#: Additive signal weights for :func:`_signal_score` — higher means keep HOT. ``user``
#: and BINDING dominate so they are archived only if the budget forces it; inbound
#: citations and recency add the rest; a per-type floor breaks ties.
_SIGNAL_USER = 1000
_SIGNAL_BINDING = 500
_SIGNAL_PER_INBOUND_LINK = 40
_SIGNAL_RECENT = 200
_TYPE_WEIGHTS = {"feedback": 90, "retro": 70, "reference": 30, "project": 20, "user": 10, "other": 10}

#: A RESOLVED per-ticket record — ``ticket-<n>-reviewed-merge-safe.md``,
#: ``ticket-<n>-done.md``. Anchored with a REQUIRED ticket number, because a
#: ``ticket-plan-*.md`` name is a standing rule ABOUT ticket plans — it merely starts with
#: the same word — and must not be swept up with the settled history (#4385).
_SETTLED_TICKET_RECORD_RE = re.compile(r"^ticket-\d+[-.]")


def over_budget(byte_size: int, line_count: int) -> bool:
    """Whether an index of *byte_size* bytes / *line_count* lines is over the gate-(d) budget.

    The one place the §4 gate-(d) budgets are compared, so the decay-pressure trigger and
    the gate that grades the result can never disagree on "over budget" (#2723). The
    loader truncates ``MEMORY.md`` on BOTH axes, so either alone is over: bytes at ~24 KB
    (#2755) and lines at 200 (#4057). Reading only bytes leaves decay idle under line
    pressure — precisely the case where a comfortable byte figure certifies a truncated
    file.
    """
    from teatree.loops.dream.gates import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        INDEX_BYTE_BUDGET,
        INDEX_LINE_BUDGET,
    )

    return byte_size > INDEX_BYTE_BUDGET or line_count > INDEX_LINE_BUDGET


def under_drain_target(byte_size: int, line_count: int) -> bool:
    """Whether an index of *byte_size* / *line_count* has drained to the budget-tier TARGET.

    The sibling of :func:`over_budget`, reading the drain targets rather than the budgets:
    the budget is the CEILING gate (d) grades, this is where the tier STOPS. Stopping on
    the ceiling landed the index on exactly 200 lines with zero headroom, so the first
    memory written after the pass truncated the tail and it stayed truncated until the next
    nightly pass (#4385). The two predicates are deliberately NOT the same call — the tier
    still FIRES on the ceiling (:func:`index_over_budget`) and drains to here, which is the
    hysteresis that keeps a corpus sitting between the two from being archived nightly for
    nothing (#2755).
    """
    from teatree.loops.dream.gates import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        INDEX_BYTE_DRAIN_TARGET,
        INDEX_LINE_DRAIN_TARGET,
    )

    return byte_size <= INDEX_BYTE_DRAIN_TARGET and line_count <= INDEX_LINE_DRAIN_TARGET


def index_over_budget(index_text: str) -> bool:
    """Whether the rendered ``MEMORY.md`` exceeds either gate-(d) session-load budget."""
    return over_budget(len(index_text.encode("utf-8")), len(index_text.splitlines()))


def is_settled_ticket_record(memory: MemoryFile) -> bool:
    """True for a resolved per-ticket record — settled history, archived out of the hot index first.

    The hot index exists to surface STANDING rules, but settled per-ticket records
    accumulate monotonically while standing rules do not, so an index ordered on signal
    alone drifts toward being mostly history. The drift is not merely passive: the
    cross-link phase has no fan-out cap, so these near-identical records become a
    mutually-citing clique and each collects +40 x ~30 of inbound-link signal — ranking
    every one of them ABOVE an uncited durable rule. Measured on a real ~220-memory corpus,
    that inversion spent 12 of a 26-file drain on durable standing rules (#4385).

    Fails CLOSED toward RETENTION: anything this cannot classify is NOT settled, so it
    keeps its normal signal score and is archived only if the budget forces it. The costs
    are asymmetric — a false positive archives a live standing rule and stops it
    influencing behaviour until somebody notices, a false negative costs one index line, of
    which the drain target now provides ~60.

    The ticket NUMBER is required: a ``ticket-plan-*`` name is a standing rule ABOUT ticket
    plans, not a record of one.
    """
    return _SETTLED_TICKET_RECORD_RE.match(memory.path.name) is not None


def _resolved_type(memory: MemoryFile) -> str:
    """The memory's type for the type-weight signal.

    Frontmatter ``metadata.type`` when present and recognised, else the filename prefix
    (``feedback_x`` -> ``feedback``), else ``other``. The ~96 older files with no
    parseable ``metadata.type`` fall back to the prefix; deterministic and DB-free.
    """
    match = _TYPE_LINE_RE.search(memory.text)
    if match:
        candidate = match.group(1).strip().lower()
        if candidate in _KNOWN_TYPES:
            return candidate
    prefix = memory.path.stem.split("_", 1)[0].lower()
    return prefix if prefix in _KNOWN_TYPES else "other"


def _is_user_memory(memory: MemoryFile) -> bool:
    """True for a user-authored memory — frontmatter ``metadata.type: user`` OR a ``user_*`` filename."""
    return _resolved_type(memory) == "user" or memory.path.name.lower().startswith("user_")


def _recency_score(memory: MemoryFile, now: datetime, retention: timedelta) -> int:
    """Recency signal — +200 within the retention window, decaying linearly past it.

    Floored at 0. Reads the logical ``lesson_touched`` clock so a cross-link / re-index
    rewrite (which bumps ``st_mtime``) does not reset recency.
    """
    age = now - memory.lesson_touched
    if age <= retention:
        return _SIGNAL_RECENT
    return max(0, _SIGNAL_RECENT - (age - retention).days)


def signal_score(memory: MemoryFile, *, inbound_links: int, now: datetime, retention: timedelta) -> int:
    """The keep-HOT signal of a memory — higher means more worth keeping in ``MEMORY.md``.

    Composed ADDITIVELY (never short-circuits) from the signals that mark a lesson
    load-bearing: a user-authored memory (+1000), BINDING / Non-Negotiable doctrine
    (+500), each inbound ``[[name]]`` wikilink (+40, *inbound_links* precomputed by the
    caller via :func:`_inbound_link_counts` so scoring the whole set stays O(N)), recency
    by the logical ``lesson_touched`` clock (+200 within *retention*, decaying linearly
    with age beyond it), and a per-type floor (feedback 90 / retro 70 / reference 30 /
    project 20 / other 10). The budget tier archives LOWEST score first, so the
    highest-signal memories stay hot and user / BINDING entries are archived only if the
    budget forces it. DB-free and deterministic — usable under ``SimpleTestCase``.
    """
    score = _SIGNAL_USER if _is_user_memory(memory) else 0
    if is_binding_text(memory.text):
        score += _SIGNAL_BINDING
    score += _SIGNAL_PER_INBOUND_LINK * inbound_links
    score += _recency_score(memory, now, retention)
    score += _TYPE_WEIGHTS.get(_resolved_type(memory), _TYPE_WEIGHTS["other"])
    return score


@dataclass(frozen=True, slots=True)
class BudgetProjection:
    """Everything the budget walk measures against, beyond the file set itself.

    One value rather than five loose parameters, because they are one fact: the memory
    dir as the re-index will find it. ``memory_dir`` in particular is here to resolve the
    ``MEMORY_PRIORITY.md`` preamble, and a caller able to supply the other four while
    forgetting that one is exactly how the projection came to model a file nobody writes.
    """

    memory_dir: Path
    index_text: str
    citers: Mapping[str, tuple[str, ...]]
    now: datetime
    retention: timedelta


def budget_tier_candidates(files: Sequence[MemoryFile], projection: BudgetProjection) -> Iterable[MemoryFile]:
    """Yield budget-tier archival candidates settled-history first, then lowest-signal.

    Fires only when the live ``MEMORY.md`` is over the gate-(d) CEILING, and drains to the
    lower :data:`~teatree.loops.dream.gates.INDEX_LINE_DRAIN_TARGET` /
    :data:`~teatree.loops.dream.gates.INDEX_BYTE_DRAIN_TARGET`. The asymmetry is deliberate
    hysteresis (#4385): stopping ON the ceiling left the index at exactly the budget, so the
    first memory written after the pass truncated the tail and it stayed truncated until the
    next nightly pass, while gate (d) — which grades once, immediately after the pass —
    reported PASS. Firing on the target instead would archive a file a night forever.

    Candidates are ordered SETTLED-HISTORY-FIRST (:func:`is_settled_ticket_record`), then by
    :func:`signal_score` within each tier. Signal alone inverts on the real corpus: the
    uncapped cross-link fan-out makes the resolved per-ticket records a mutually-citing
    clique worth ~+1200 of inbound-link signal each, ranking every one of them above an
    uncited durable rule, so the lowest-first walk archived the standing rules the index
    exists to surface and left the settled history hot.

    A referenced file (a live consumer still ``[[link]]``s it) is NOT hard-retained here
    (#2753): the cross-link phase runs before decay and references most of the corpus, so a
    hard skip floored the tier above the referenced count and the index could never reach
    budget. Instead :func:`signal_score` adds +40 per inbound ``[[name]]`` link, so referenced
    entries rank HIGHER and are archived LAST — only when the budget genuinely forces it.
    After each removal the survivor set's PROJECTED index — rendered exactly as the
    re-index will render it — is re-measured on BOTH axes, and the walk STOPS as soon as
    it has drained to the byte AND line targets, so the MINIMUM number of files is
    archived and as much high-signal memory as fits stays hot. user / BINDING entries
    score highest and are archived only if the budget forces it. Every archived entry
    stays restorable (full body in ``archive/`` with provenance) and recall-able (its
    signature in the cold ``MEMORY_ARCHIVE.md``); a now-dangling ``[[link]]`` in a
    surviving body is cosmetic, not data loss — the hot index uses bare ``- name.md``
    pointers, which never dangle. The conservative stale/ledger tier
    (:func:`_stale_candidates`) keeps its reference skip — only the budget tier drops it.

    The projected header is rendered with the SAME ``MEMORY_PRIORITY.md`` preamble the
    re-index emits (:func:`teatree.loops.dream.reindex.read_priority_preamble`), which is
    why *memory_dir* is a parameter. Projecting the generated ~180-byte header instead
    modelled a file that is never written: :func:`~teatree.loops.dream.reindex.render_index`
    REPLACES that header with the human-owned block, so on any box carrying a preamble the
    projection under-counted the index by the preamble's whole size. The walk then read
    the very first survivor set as already fitting and archived ZERO files, while the
    index it was projecting stayed over budget — gate (d) failing every night with nothing
    naming the cause.
    """
    if not index_over_budget(projection.index_text):
        return
    from teatree.loops.dream import reindex  # noqa: PLC0415 — deferred: loaded at tick time, not import

    ordered = sorted(
        files,
        key=lambda m: (
            0 if is_settled_ticket_record(m) else 1,
            signal_score(
                m,
                inbound_links=len(projection.citers.get(m.path.name, ())),
                now=projection.now,
                retention=projection.retention,
            ),
        ),
    )
    line_bytes = {m.path: len(reindex.index_line_for(m.path.name).encode("utf-8")) for m in files}
    header = reindex.render_index_lines([], reindex.read_priority_preamble(projection.memory_dir))
    header_bytes = len(header.encode("utf-8"))
    header_lines = len(header.splitlines())
    survivor_count = len(files)
    survivor_bytes = sum(line_bytes.values())
    for memory in ordered:
        # Both projections are EXACT against render_index_lines(survivor lines) for any
        # count: the per-line "\n" join + trailing newline total ``survivor_count`` bytes,
        # and each survivor contributes exactly one line past the header's own.
        projected_bytes = header_bytes + survivor_bytes + survivor_count
        if under_drain_target(projected_bytes, header_lines + survivor_count):
            break  # projected survivor index has drained to BOTH targets — archive no more
        survivor_count -= 1
        survivor_bytes -= line_bytes[memory.path]
        yield memory


__all__ = [
    "BudgetProjection",
    "budget_tier_candidates",
    "index_over_budget",
    "is_settled_ticket_record",
    "over_budget",
    "signal_score",
    "under_drain_target",
]
