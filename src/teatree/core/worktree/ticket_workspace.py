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

:func:`assert_joins_ticket_workspace` is deliberately scoped to a ticket that
ALREADY has a settled workspace: a ticket with no materialised worktree yet, or
one whose worktrees already disagree, has no single workspace to join, so the
assertion is a no-op and never converts a pre-existing split into a hard failure
at an unrelated call site. PROVISIONING is the one caller that must separate
those two states — :func:`ticket_workspace_dir_or_refuse` falls back on the first
and refuses on the second, because materialising the next repo would deepen the
split it cannot see.
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


def _group_equivalent_dirs(parents: set[Path]) -> set[Path]:
    """Collapse *parents* to one representative per :func:`paths_match` class.

    A ``/var`` candidate and its ``/private/var`` twin — or a configured symlink
    and its target — are two spellings of ONE workspace dir. Counting them raw
    reports a healthy ticket as split and refuses a registration that would
    actually land as a sibling.
    """
    groups: list[Path] = []
    for candidate in parents:
        if not any(paths_match(candidate, existing) for existing in groups):
            groups.append(candidate)
    return set(groups)


def ticket_workspace_dirs(ticket: "Ticket") -> set[Path]:
    """Every distinct parent dir *ticket*'s materialised worktrees sit in.

    A repo worktree lives at ``<ticket-dir>/<repo-leaf>``, so the parent of any
    one of them IS a ticket dir. Only paths that are still directories count, so a
    torn-down worktree's stale row cannot pin the ticket to a dir that no longer
    exists. Empty means nothing is materialised yet; more than one (after
    :func:`_group_equivalent_dirs` collapses path-spelling variants) means the
    ticket is genuinely split.
    """
    raw = {
        Path(path).parent
        for wt in Worktree.objects.for_ticket(ticket)
        if (path := (wt.extra or {}).get("worktree_path")) and Path(path).is_dir()
    }
    return _group_equivalent_dirs(raw)


def ticket_workspace_dir(ticket: "Ticket") -> Path | None:
    """The single directory holding *ticket*'s materialised worktrees, or ``None``.

    Returns ``None`` when the ticket has no on-disk worktree yet (nothing to join)
    or when the existing ones disagree on a parent (a pre-existing split this
    predicate refuses to paper over by picking a winner).
    """
    parents = ticket_workspace_dirs(ticket)
    return parents.pop() if len(parents) == 1 else None


def ticket_workspace_dir_or_refuse(ticket: "Ticket") -> Path | None:
    """Like :func:`ticket_workspace_dir`, but a SPLIT refuses instead of answering ``None``.

    ``None`` from that predicate covers two states whose correct handling is
    opposite: "nothing materialised yet", where a caller must fall back to its
    own default, and "the worktrees disagree", where falling back adds the next
    repo to one arbitrary side and deepens the split. A caller that provisions
    calls this one so the second state stops it.
    """
    parents = ticket_workspace_dirs(ticket)
    if len(parents) > 1:
        raise TicketWorkspaceDivergenceError(_split_refusal(ticket, parents))
    return parents.pop() if parents else None


def _split_refusal(ticket: "Ticket", parents: set[Path]) -> str:
    """Name every offending root verbatim — the operator cannot act on a count.

    Deliberately does NOT prescribe ``workspace relocate``: relocation is a
    ``rename(2)``, which these roots' bind mounts refuse across their boundary, so
    prescribing it would be a remedy that cannot discharge its own finding.
    Prescribes ``workspace repair-split``, not ``workspace clean-all``: clean-all
    is the DONE-worktree reaper — it deliberately keeps an unfinished checkout — so
    it repairs nothing on a still-open ticket. Repair MOVES each checkout instead
    of reaping it, so unfinished work survives.
    """
    roots = ", ".join(sorted(str(p) for p in parents))
    return (
        f"Refusing to provision ticket {ticket.pk}: its worktrees are split across {len(parents)} workspace "
        f"dirs ({roots}), and a ticket's repos must be siblings in ONE dir so each can resolve the others "
        f"(a split ticket silently drops services from the generated stack). Provisioning would add the next "
        f"repo to one arbitrary side. Repair it from the venue that mounts these checkouts "
        f"(`t3 <overlay> workspace repair-split` inside the container), then re-provision."
    )


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
