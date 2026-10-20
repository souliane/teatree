"""The ``worktree`` occupancy-claim operator commands (#3952).

A :class:`OccupancyCommands` mixin the ``worktree``
:class:`~django_typer.management.TyperCommand` inherits from, so the leaves mount
under ``t3 <overlay> worktree occupancy`` / ``claim-occupancy`` /
``release-occupancy`` while their LOC stays out of the cap-bound ``worktree.py``
— the same split as :class:`PlanCommands`. django-typer collects ``@command``
methods from every ``TyperCommand`` base in the MRO, so the CLI surface is
unchanged by where they live.

These are the surface a lane that teatree does NOT dispatch reaches the claim
through: an operator (or an agent working a branch by hand at the raw-git level)
takes the claim before editing and hands it back after, so the factory's own
dispatch sees the tree as occupied instead of walking into it. A dispatched agent
never needs them — ``run_headless`` holds the claim for the whole run.
"""

from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.management.commands._task_cancel_auth import direct_owner_action
from teatree.core.models import Worktree
from teatree.core.worktree.checkout_attest import AttestRefusedError, attest_checkout
from teatree.core.worktree.occupancy import WorktreeOccupiedError, acquire, held_worktrees, occupancy_holder, release
from teatree.core.worktree.worktree_paths import paths_match


def _worktree_for_path(raw: str) -> Worktree:
    """The registered ``Worktree`` whose checkout is *raw*, or a refusal naming the miss."""
    candidate = Path(raw).expanduser()
    resolved = str(candidate.resolve())
    resolved_match: Worktree | None = None
    alias: Worktree | None = None
    for worktree in Worktree.objects.exclude(extra__worktree_path__isnull=True).order_by("pk"):
        recorded = worktree.worktree_path
        if recorded == str(candidate):
            return worktree
        if recorded == resolved:
            resolved_match = worktree
        if recorded and alias is None and paths_match(recorded, candidate):
            alias = worktree
    if resolved_match is not None:
        return resolved_match
    if alias is not None:
        return alias
    typer.echo(
        f"  Refused: no registered worktree is recorded at {candidate}. "
        "List the live claims with `t3 <overlay> worktree occupancy`.",
        err=True,
    )
    raise SystemExit(1)


class OccupancyCommands(TyperCommand):
    """The checkout-occupancy operator command surface (mixed into the ``worktree`` command)."""

    @command()
    def occupancy(self) -> None:
        """Show every checkout a live agent currently holds (#3952)."""
        held = held_worktrees()
        if not held:
            self.stdout.write("  No checkout is currently held.")
            return
        for worktree, holder in held:
            self.stdout.write(f"  {worktree.worktree_path or '<unprovisioned>'} — {holder.describe()}")

    @command()
    def attest(self, path: str) -> None:
        """Record the present checkout state after the owner has inspected it."""
        if not direct_owner_action():
            self.stderr.write("  Refused: only a direct owner command may attest a checkout.")
            raise SystemExit(1)
        worktree = _worktree_for_path(path)
        try:
            attestation = attest_checkout(worktree)
        except AttestRefusedError as exc:
            self.stderr.write(f"  Refused: {path} {exc}")
            raise SystemExit(1) from exc
        suffix = " (one dispatch; host process evidence unavailable)" if not attestation.host_evidence else ""
        self.stdout.write(f"  attested {worktree.worktree_path} at {attestation.state.digest}{suffix}")

    @command(name="claim-occupancy")
    def claim_occupancy(
        self,
        path: str,
        *,
        holder: Annotated[str, typer.Option("--holder", help="Identity recorded as the occupant.")] = "",
        holder_session: Annotated[
            str, typer.Option("--holder-session", help="Session qualifier, so two runs of one holder differ.")
        ] = "",
        lease_seconds: Annotated[int, typer.Option("--lease-seconds", help="Override the configured claim TTL.")] = 0,
    ) -> None:
        """Claim a checkout for a hand-driven lane, or refuse naming who already holds it."""
        if not holder.strip():
            self.stderr.write("  Refused: --holder is required (an anonymous claim names nobody to wait on)")
            raise SystemExit(1)
        worktree = _worktree_for_path(path)
        try:
            claimed = acquire(
                worktree,
                holder=holder.strip(),
                holder_session=holder_session.strip(),
                lease_seconds=lease_seconds or None,
            )
        except WorktreeOccupiedError as exc:
            self.stderr.write(f"  Refused: {exc}")
            raise SystemExit(1) from exc
        self.stdout.write(f"  claimed {worktree.worktree_path} for {claimed.describe()}")

    @command(name="release-occupancy")
    def release_occupancy(self, path: str) -> None:
        """Hand a checkout back, naming whose claim was freed.

        Frees whoever currently holds it rather than only the caller's own claim:
        this is the operator's escape for a holder that died without releasing,
        and it is the command every refusal points at. It touches the four claim
        columns and nothing else — no directory, branch, container or process.
        """
        worktree = _worktree_for_path(path)
        current = occupancy_holder(worktree)
        if current is None:
            self.stdout.write(f"  {worktree.worktree_path} is not held — nothing to release.")
            return
        release(worktree, holder=current.holder, holder_session=current.holder_session)
        self.stdout.write(f"  released {worktree.worktree_path} (was held by {current.describe()})")
