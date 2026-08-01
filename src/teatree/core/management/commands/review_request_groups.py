"""``t3 review-request group-status`` — what each work group is waiting for (R1).

The reading half of :mod:`~teatree.core.gates.review_request_batch_gate`. The
gate answers one merge request's question at a post chokepoint and says only
"held"; an operator looking at a quiet review channel needs the other shape —
every group, every member, and the exact reason each one is not moving.

Strictly read-only. It posts nothing, claims nothing, and raises no owner
question, so it is safe to run at any point in a batch. For a group that IS
ready it prints the ordered ``t3 review-request post`` lines rather than
performing them, keeping the decision to broadcast with the operator.

An unreadable forge exits non-zero. A status command that reported "no groups"
because the listing failed would read exactly like a genuinely empty queue,
which is the one answer an operator must never be handed by accident.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.gates.review_request_batch_gate import (
    GROUP_TOO_LARGE,
    GROUP_UNRESOLVED,
    HOST_UNAVAILABLE,
    HOST_UNREADABLE,
    BatchVerdict,
    MemberReadiness,
    post_command_lines,
    work_groups,
)
from teatree.core.gates.review_request_guard import canonical_mr_url, overlay_for_mr_url

_UNREADABLE_REASONS = frozenset({HOST_UNAVAILABLE, HOST_UNREADABLE})

_REASON_HELP = {
    HOST_UNAVAILABLE: "no code host is configured for this overlay, so no group can be resolved.",
    HOST_UNREADABLE: "the open merge request listing could not be read, so every group is held.",
    GROUP_UNRESOLVED: "this merge request is not in the operator's open listing.",
    GROUP_TOO_LARGE: "the group is above work_group_max_members — the owner has been asked about it.",
}


class Command(TyperCommand):
    @command()
    def handle(
        self,
        mr_url: Annotated[
            str,
            typer.Option("--mr-url", help="Show only the work group holding this merge request."),
        ] = "",
    ) -> None:
        """Print each work group's readiness, its blockers, and how to broadcast it.

        Never posts. Exits ``1`` when the forge listing could not be read, ``0``
        otherwise — a held group is a normal state to report, not a failure.
        """
        verdicts = work_groups(overlay_name=overlay_for_mr_url(mr_url) if mr_url else "")
        unreadable = [verdict for verdict in verdicts if verdict.reason in _UNREADABLE_REASONS]
        if unreadable:
            self.stderr.write(f"work groups unavailable: {_REASON_HELP[unreadable[0].reason]}")
            raise SystemExit(1)

        selected = self._select(verdicts, mr_url)
        if not selected:
            self.stdout.write(self._nothing_to_show(mr_url))
            return
        for verdict in selected:
            self._write_group(verdict)

    @staticmethod
    def _select(verdicts: tuple[BatchVerdict, ...], mr_url: str) -> tuple[BatchVerdict, ...]:
        if not mr_url:
            return verdicts
        canonical = canonical_mr_url(mr_url)
        return tuple(verdict for verdict in verdicts if any(member.mr_url == canonical for member in verdict.members))

    @staticmethod
    def _nothing_to_show(mr_url: str) -> str:
        if mr_url:
            return f"no open work group holds {canonical_mr_url(mr_url)}."
        return "no open merge requests, so no work groups."

    def _write_group(self, verdict: BatchVerdict) -> None:
        state = "READY" if verdict.ready else "HOLDING"
        self.stdout.write(f"\nwork group {verdict.group_key} — {state} ({len(verdict.members)} members)")
        if verdict.reason in _REASON_HELP:
            self.stdout.write(f"  {_REASON_HELP[verdict.reason]}")
        for member in verdict.members:
            self.stdout.write(f"  {self._member_line(member)}")
        lines = post_command_lines(verdict)
        if lines:
            self.stdout.write("  broadcast the whole group, in this order:")
            for line in lines:
                self.stdout.write(f"    {line}")

    @staticmethod
    def _member_line(member: MemberReadiness) -> str:
        exempt = " [review-exempt]" if member.review_exempt else ""
        held_by = "" if member.ready else " — " + ", ".join(_blocker_code(code) for code in member.blockers)
        return f"{'READY  ' if member.ready else 'HOLDING'} {member.mr_url}{exempt}{held_by}"


def _blocker_code(blocker: str) -> str:
    """The bare axis code — the group listing already names the merge request."""
    return blocker.rpartition(": ")[2]
