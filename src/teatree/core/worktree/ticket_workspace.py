"""The ONE workspace directory a ticket's worktrees share — same ticket, same workspace.

A multi-repo ticket materialises one worktree per repo as SIBLINGS inside a single
ticket directory (``<worktree-root>/<branch>/<repo-leaf>``, the layout
:mod:`teatree.core.worktree.worktree_paths` owns). The provisioner has always
honoured that shape: :func:`ticket_workspace_dir` is the predicate it uses to
co-locate a repo added later next to the ticket's existing worktrees rather than
opening a fresh ticket dir.

The AD-HOC registration seams did not honour it. ``adopt_worktree_for_ticket`` and
the cwd resolver each record whatever directory they are pointed at, with no
reference to where the ticket's other repos already live, so one ticket could end
up with its repos split across two roots.

That split is not a cosmetic layout preference — it silently removes services.
Overlay code resolves a worktree's sibling repos by scanning the worktree's own
parent dir, so when the frontend worktree lives under a different parent the
backend's generated compose override simply has no frontend service in it, and
browser E2E has nothing to hit. Provisioning logs that as a warning and reports
success, which is how a half-provisioned stack reads as green. Hence the refusal
here: a divergence must FAIL where the row is registered, loudly, instead of
surviving as a warning nobody reads.

The refusal is deliberately scoped to a ticket that ALREADY has a settled
workspace. A ticket with no materialised worktree yet (first provision), or one
whose existing worktrees already disagree about their parent, has no single
workspace to join — :func:`ticket_workspace_dir` returns ``None`` for both and the
assertion is a no-op, so this never converts a pre-existing split into a hard
failure at an unrelated call site. Draining those is the reaper's job, not this
predicate's.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.models import Worktree
from teatree.core.worktree.worktree_paths import paths_match

if TYPE_CHECKING:
    from teatree.core.models import Ticket


class TicketWorkspaceDivergenceError(RuntimeError):
    """Raised when a worktree would join a ticket from outside its one workspace dir.

    Carries the established workspace dir and the offending candidate so the
    caller can surface both verbatim — an operator's next action is to move the
    checkout into the named directory (or to relocate the whole ticket), and
    neither is guessable from "divergent path".
    """


def ticket_workspace_dir(ticket: "Ticket") -> Path | None:
    """The single directory holding *ticket*'s materialised worktrees, or ``None``.

    A repo worktree lives at ``<ticket-dir>/<repo-leaf>``, so the parent of any
    one of them IS the ticket dir. Returns ``None`` when the ticket has no
    on-disk worktree yet (nothing to join) or when the existing ones disagree on
    a parent (a pre-existing split this predicate refuses to paper over by
    picking a winner). Only paths that are still directories count, so a torn-down
    worktree's stale row cannot pin the ticket to a dir that no longer exists.
    """
    parents = {
        Path(path).parent
        for wt in Worktree.objects.for_ticket(ticket)
        if (path := (wt.extra or {}).get("worktree_path")) and Path(path).is_dir()
    }
    return parents.pop() if len(parents) == 1 else None


def assert_joins_ticket_workspace(ticket: "Ticket", candidate: Path) -> None:
    """Refuse *candidate* unless it sits in *ticket*'s established workspace dir.

    The structural half of "same ticket → same workspace": every seam that
    REGISTERS a worktree row calls this, so joining a ticket from a foreign root
    fails at registration rather than producing a ticket whose repos cannot see
    each other. Symlink-tolerant via :func:`paths_match` — a ``/var`` candidate
    must match its ``/private/var`` twin, or a macOS caller would trip the
    refusal on two spellings of one directory.
    """
    workspace = ticket_workspace_dir(ticket)
    if workspace is None or paths_match(workspace, candidate.parent):
        return
    msg = (
        f"Refusing to register {candidate} on ticket {ticket.pk}: the ticket's worktrees live in "
        f"{workspace}, and a ticket's repos must be siblings in ONE workspace dir so each can "
        f"resolve the others (a split ticket silently drops services from the generated stack). "
        f"Move the checkout to {workspace / candidate.name}, or relocate the whole ticket with "
        f"`t3 <overlay> workspace relocate`."
    )
    raise TicketWorkspaceDivergenceError(msg)
