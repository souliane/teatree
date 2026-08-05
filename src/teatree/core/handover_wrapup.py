"""The sub-agent barrier's returns, as ROW STATE rendered onto the delivery surface.

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
(:attr:`~teatree.core.models.SessionHandover.subagent_wrapup`). Bounded by the number
of distinct agents rather than the number of hand-offs, and lossless: an agent
enumerated at hand-off #1 and absent at #5 is still named, because absence is
ambiguous — it either finished cleanly or its worktree is gone, and the second is the
highest-risk thing a hand-off carries.

The union is DELIVERY state rendered ONTO the payload at delivery
(:func:`delivered_payload`), never spliced INTO it. Splicing had to decide which bytes
were the harness's own by matching marker text, which is a claim about somebody else's
prose: an authored body quoting a marker had that region deleted, and an unterminated
quote took the whole tail with it. Nothing here reads the payload to learn a fact about
the harness, and nothing writes into it.

The row also records whether and when a barrier ran
(:attr:`~teatree.core.models.SessionHandover.last_barrier_at`,
:attr:`~teatree.core.models.SessionHandover.barrier_ran_at_latest_handoff`), because
``[]`` means "no agents" and NULL means "nobody looked". Conflating them is what let a
hand-off that ran no barrier state an earlier barrier's finding as its own.
"""

from operator import itemgetter
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence

    from teatree.core.handover_orchestration import SubagentPush
    from teatree.core.models.session_handover import SessionHandover

__all__ = [
    "SUBAGENT_SECTION_HEADER",
    "SubagentRecord",
    "delivered_payload",
    "merge_subagent_records",
    "record_barrier_returns",
    "render_subagent_section",
    "subagent_record",
    "wrapup_section_for",
]

SUBAGENT_SECTION_HEADER = "## Sub-agent wrap-up"


class SubagentRecord(TypedDict):
    """One agent's entry in the union stored on :attr:`SessionHandover.subagent_wrapup`.

    JSON round-trips as a plain ``dict``, so the persisted row compares equal to the
    records the command merged — which is the completeness check.
    """

    worktree: str
    branch: str
    done: str
    remaining: str
    first_seen_at: str
    last_seen_at: str
    in_latest_barrier: bool


def subagent_record(push: "SubagentPush", *, at: "dt.datetime") -> SubagentRecord:
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


def merge_subagent_records(
    existing: "Sequence[SubagentRecord]", incoming: "Sequence[SubagentRecord]"
) -> list[SubagentRecord]:
    """The union of *existing* and *incoming*, keyed on worktree — pure, no ORM.

    An agent in *incoming* takes that barrier's branch/done/remaining and keeps its
    original ``first_seen_at``. An agent only in *existing* is preserved untouched
    except for ``in_latest_barrier``, which goes false: its status is the last known
    one, and its worktree may since have been pruned.
    """
    merged: dict[str, SubagentRecord] = {}
    for record in existing:
        stale = record.copy()
        stale["in_latest_barrier"] = False
        merged[record["worktree"]] = stale
    for record in incoming:
        prior = merged.get(record["worktree"])
        fresh = record.copy()
        fresh["first_seen_at"] = (prior or record)["first_seen_at"]
        merged[record["worktree"]] = fresh
    return sorted(merged.values(), key=itemgetter("first_seen_at", "worktree"))


def render_subagent_section(
    records: "Sequence[SubagentRecord]", *, last_barrier_at: "dt.datetime | None", barrier_ran_at_latest_handoff: bool
) -> str:
    """The union as the section the receiver reads — ``""`` when no barrier has ever run.

    Each agent contributes what it finished and what is left, because "remaining"
    is the half the receiver has to act on. An agent absent from the latest barrier
    is marked as such rather than silently dropped OR silently presented as current.

    Every case NAMES the barrier its counts belong to. A row whose latest hand-off ran
    none says so and gives the instant of the last one that did, so a reader can see
    what has not been re-checked since — the alternative is a hand-off restating an
    earlier barrier's finding as its own. Silence is reserved for the one case where it
    is the true statement: nobody has ever looked.
    """
    if not records and last_barrier_at is None:
        return ""
    if not records and barrier_ran_at_latest_handoff:
        return (
            f"{SUBAGENT_SECTION_HEADER} (0 agents)\n\n"
            "A sub-agent barrier ran at this hand-off and found no in-flight sub-agent "
            "worktree carrying pending work."
        )
    since = last_barrier_at.isoformat() if last_barrier_at else "an unrecorded instant"
    if not records:
        return (
            f"{SUBAGENT_SECTION_HEADER} (0 agents; NO barrier ran at this hand-off)\n\n"
            f"The last barrier ran at {since} and found none. This hand-off ran none, so "
            "nothing has been re-checked since — a sub-agent started after that instant is "
            "NOT covered by this line."
        )
    if barrier_ran_at_latest_handoff:
        latest = sum(1 for record in records if record["in_latest_barrier"])
        header = f"{len(records)} agents seen; {latest} enumerated at THIS hand-off's barrier"
    else:
        header = f"{len(records)} agents seen; NO barrier ran at this hand-off — the last was at {since}"
    lines = [f"{SUBAGENT_SECTION_HEADER} ({header})", ""]
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


def record_barrier_returns(
    handover: "SessionHandover", records: "Sequence[SubagentRecord]", *, at: "dt.datetime", barrier_ran: bool
) -> None:
    """Store the barrier's returns AND the barrier fact on *handover*. Touches no payload byte.

    Unconditional: recording is a non-destructive row write, so there is no branch left
    for an authored body to forge the harness into taking.
    """
    handover.subagent_wrapup = list(records)
    handover.barrier_ran_at_latest_handoff = barrier_ran
    if barrier_ran:
        handover.last_barrier_at = at
    handover.save(update_fields=["subagent_wrapup", "last_barrier_at", "barrier_ran_at_latest_handoff"])


def wrapup_section_for(handover: "SessionHandover") -> str:
    """*handover*'s wrap-up section, rendered from the ROW — never from its payload text."""
    return render_subagent_section(
        handover.subagent_wrapup,
        last_barrier_at=handover.last_barrier_at,
        barrier_ran_at_latest_handoff=handover.barrier_ran_at_latest_handoff,
    )


def delivered_payload(handover: "SessionHandover") -> str:
    """The row's payload with the wrap-up section appended — the bytes a receiver reads.

    One seam, so the XDG mirror and the claimed payload can never disagree about what
    was delivered, and the section is last by construction rather than by a caller
    contract a later absorb could falsify.
    """
    section = wrapup_section_for(handover)
    return f"{handover.payload.rstrip()}\n\n{section}" if section else handover.payload


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
