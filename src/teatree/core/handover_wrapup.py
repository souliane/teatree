"""The sub-agent barrier's returns, as the ONE payload section a receiver reads.

Split out of :mod:`teatree.core.handover` (which owns payload resolution, target
resolution and the XDG mirror) so the wrap-up is one self-describing unit: what a
barrier saw, how it renders, and how it lands on the persisted row.

The returns used to be PRINTED only, so the row — which is what a receiving
session actually gets — carried none of the obligations the barrier collected.
Folding them in then appended a section per hand-off, so a session that handed
off five times left five sections, each a snapshot of a different moment. The
receiver's question is not "what did the barrier see at instant T?" but, per agent
worktree: "where is that agent's work now, and what is still owed?" — one answer
per agent, not N snapshots to reconcile.

So the row carries a stored UNION of every agent any of its barriers enumerated
(:attr:`~teatree.core.models.SessionHandover.subagent_wrapup`), and the section is
rendered from it. Bounded by the number of distinct agents rather than the number
of hand-offs, and lossless: an agent enumerated at hand-off #1 and absent at #5 is
still named, because absence is ambiguous — it either finished cleanly or its
worktree is gone, and the second is the highest-risk thing a hand-off carries.
"""

from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.handover import write_mirror
from teatree.core.session_handover_manager import block_markers, upsert_payload_block

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence

    from teatree.core.handover_orchestration import SubagentPush
    from teatree.core.models.session_handover import SessionHandover

__all__ = [
    "SUBAGENT_BLOCK_MARKER",
    "SUBAGENT_MARKER_END",
    "SUBAGENT_MARKER_START",
    "SUBAGENT_SECTION_HEADER",
    "carries_subagent_section",
    "merge_subagent_records",
    "render_subagent_section",
    "subagent_record",
    "upsert_subagent_section",
]

SUBAGENT_SECTION_HEADER = "## Sub-agent wrap-up"
#: One home for the delimiter, so the renderer, the upsert and the completeness
#: assertion can never disagree about what bounds the block.
SUBAGENT_BLOCK_MARKER = "t3:handover:subagents"
SUBAGENT_MARKER_START, SUBAGENT_MARKER_END = block_markers(SUBAGENT_BLOCK_MARKER)


def subagent_record(push: "SubagentPush", *, at: "dt.datetime") -> dict:
    """One agent's barrier return, as the record the union is keyed on.

    The resolved worktree path is the identity: it is stable across barriers,
    unlike the branch, which a fast-push may create or rename.
    """
    stamp = at.isoformat()
    return {
        "worktree": str(push.worktree),
        "branch": push.branch,
        "done": _push_done(push),
        "remaining": _push_remaining(push),
        "first_seen_at": stamp,
        "last_seen_at": stamp,
        "in_latest_barrier": True,
    }


def carries_subagent_section(handover: "SessionHandover") -> bool:
    """Whether *handover*'s payload already carries a wrap-up block from some earlier barrier.

    A row that carries one has to be re-rendered by every later hand-off, barrier or
    no barrier: the block asserts which agents the LATEST barrier saw, so leaving it
    untouched across a hand-off that ran none re-states that freshness for a barrier
    that never happened.
    """
    return SUBAGENT_MARKER_START in handover.payload


def merge_subagent_records(existing: "Sequence[dict]", incoming: "Sequence[dict]") -> list[dict]:
    """The union of *existing* and *incoming*, keyed on worktree — pure, no ORM.

    An agent in *incoming* takes that barrier's branch/done/remaining and keeps its
    original ``first_seen_at``. An agent only in *existing* is preserved untouched
    except for ``in_latest_barrier``, which goes false: its status is the last known
    one, and its worktree may since have been pruned.
    """
    merged = {record["worktree"]: {**record, "in_latest_barrier": False} for record in existing}
    for record in incoming:
        prior = merged.get(record["worktree"])
        merged[record["worktree"]] = {**record, "first_seen_at": (prior or record)["first_seen_at"]}
    return sorted(merged.values(), key=itemgetter("first_seen_at", "worktree"))


def render_subagent_section(records: "Sequence[dict]") -> str:
    """The union as the section the receiver reads.

    Each agent contributes what it finished and what is left, because "remaining"
    is the half the receiver has to act on. An agent absent from the latest barrier
    is marked as such rather than silently dropped OR silently presented as current.

    Zero agents renders an explicit line rather than nothing: an ABSENT section is
    indistinguishable from a barrier that never ran, which is the reported symptom.
    """
    if not records:
        return (
            f"{SUBAGENT_SECTION_HEADER} (0 agents)\n\n"
            "No in-flight sub-agent worktrees carried pending work at hand-off time."
        )
    latest = sum(1 for record in records if record["in_latest_barrier"])
    lines = [f"{SUBAGENT_SECTION_HEADER} ({len(records)} agents seen; {latest} enumerated at the latest barrier)", ""]
    for record in records:
        lines += [
            f"- `{record['branch'] or '(no branch)'}` at {record['worktree']}",
            f"  - done: {record['done']}",
            f"  - remaining: {record['remaining']}",
        ]
        if not record["in_latest_barrier"]:
            lines.append(
                f"  - NOT enumerated at the latest barrier (last seen {record['last_seen_at']}) — its worktree "
                f"may be gone; the status above is the last known one."
            )
    return "\n".join(lines)


def upsert_subagent_section(handover: "SessionHandover", records: "Sequence[dict]") -> Path:
    """Store *records* on *handover*, re-render its ONE wrap-up block, re-mirror.

    ``unique_mirror_path`` keys on ``created_at``, which this does not touch, so the
    re-mirror OVERWRITES the same file and leaves ``latest`` pointed at it — one
    hand-off stays one file, with no pointer churn.
    """
    handover.subagent_wrapup = list(records)
    upsert_payload_block(handover, marker=SUBAGENT_BLOCK_MARKER, block=render_subagent_section(records))
    handover.save(update_fields=["payload", "subagent_wrapup"])
    return write_mirror(handover)


def _push_done(push: "SubagentPush") -> str:
    outcome = push.outcome
    if outcome is None:
        return "nothing — the worktree was never driven"
    done = [label for label, held in (("committed", outcome.committed), ("pushed", outcome.pushed)) if held]
    if outcome.pr_url:
        done.append(f"PR {outcome.pr_url}")
    return ", ".join(done) or "nothing"


def _push_remaining(push: "SubagentPush") -> str:
    if not push.driven:
        return push.error or "unknown error"
    outcome = push.outcome
    if outcome is None:
        return "no outcome was recorded"
    if not outcome.ok:
        return "; ".join(finding.detail for finding in outcome.findings) or "the push was refused"
    return "nothing"
