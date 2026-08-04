"""The sub-agent barrier's returns, as the payload section a receiver reads.

Split out of :mod:`teatree.core.handover` (which owns payload resolution, target
resolution and the XDG mirror) so the wrap-up is one self-describing unit: what a
barrier saw, how it renders, and how it lands on the persisted row.

The returns used to be PRINTED only, so the row — which is what a receiving
session actually gets — carried none of the obligations the barrier collected.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.handover import write_mirror
from teatree.core.session_handover_manager import append_payload

if TYPE_CHECKING:
    from collections.abc import Sequence

    from teatree.core.handover_orchestration import SubagentPush
    from teatree.core.models.session_handover import SessionHandover

__all__ = [
    "SUBAGENT_SECTION_HEADER",
    "append_subagent_section",
    "render_subagent_section",
]

SUBAGENT_SECTION_HEADER = "## Sub-agent wrap-up"


def render_subagent_section(pushes: "Sequence[SubagentPush]") -> str:
    """The barrier's per-agent returns, as the payload section the receiver reads.

    Each agent contributes what it finished and what is left, because "remaining"
    is the half the receiver has to act on.

    Zero agents renders an explicit line rather than nothing: an ABSENT section is
    indistinguishable from a barrier that never ran, which is the reported symptom.
    """
    if not pushes:
        return (
            f"{SUBAGENT_SECTION_HEADER} (0 agents)\n\n"
            "No in-flight sub-agent worktrees carried pending work at hand-off time."
        )
    lines = [f"{SUBAGENT_SECTION_HEADER} ({len(pushes)} agents)", ""]
    for push in pushes:
        lines += [
            f"- `{push.branch or '(no branch)'}` at {push.worktree}",
            f"  - done: {_push_done(push)}",
            f"  - remaining: {_push_remaining(push)}",
        ]
    return "\n".join(lines)


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


def append_subagent_section(handover: "SessionHandover", section: str) -> Path:
    """Append *section* to the persisted payload and re-mirror; return the mirror file.

    ``unique_mirror_path`` keys on ``created_at``, which this does not touch, so the
    re-mirror OVERWRITES the same file and leaves ``latest`` pointed at it — one
    hand-off stays one file, with no pointer churn.
    """
    append_payload(handover, section)
    return write_mirror(handover)
