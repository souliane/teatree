"""Refuse to reclaim a ticket's worktrees while any of its PRs/MRs is still OPEN on the forge.

``t3 <overlay> workspace teardown`` is TICKET-scoped: it reclaims *every*
worktree of the resolved ticket, siblings included. That scope is correct, and
it is only safe once the ticket is actually done — which is what this gate
decides. A ticket carrying an open, unmerged PR/MR is not done, so none of its
workspaces are reclaimable.

The FSM-automatic path carries the same rule by state: ``execute_teardown``
fires only for :meth:`Ticket.marker_release_states`, which excludes ``SHIPPED``
precisely because its PR is still open. This gate is that condition expressed
against the forge, so the operator-driven path cannot reclaim tickets the
automatic path refuses.

WHY THE FORGE IS THE AUTHORITY (and the recorded state is not)
--------------------------------------------------------------
``PullRequest`` rows are the arbiter of PR facts and ``Ticket.extra["prs"]`` is
their denormalised cache. Neither is sufficient to AUTHORISE a destructive
reclaim on its own:

* **Incomplete.** A ticket can carry an MR that was never recorded — no
    ``PullRequest`` row, no ``extra["prs"]`` entry. A gate reading only recorded
    rows is vacuous there: zero rows reads as "nothing open".
* **Stale.** ``extra["prs"]`` snapshots a ``state`` at scan time, so an entry
    can still say ``opened`` long after the MR merged. Trusting it inverts the
    gate into a permanent false block.
* **Not expressive enough.** ``PullRequest.State`` is OPEN / REVIEW_REQUESTED /
    APPROVED / MERGED with no CLOSED member, so an MR closed-without-merge stays
    non-MERGED in the model forever and a model-only gate would refuse that
    ticket's teardown for good.

Recorded rows are therefore a CANDIDATE source, never the verdict: every
non-``MERGED`` row is re-read live. ``MERGED`` is the one state taken on trust —
terminal and irreversible, so it needs no forge call.

Recorded candidates alone cannot see an unrecorded MR, so the gate adds a second
candidate source keyed on what teardown actually destroys: each worktree's own
branch. "Does an open PR/MR have this branch as its source?" is asked of the
forge directly, and is the only question that catches an MR the model never
recorded.

FAIL CLOSED
-----------
An inconclusive probe REFUSES, mirroring the sibling data-loss guard in
``_workspace.cleanup._refuse_if_unpushed``. This is deliberately the opposite of
``backends.loader.pr_is_merged_or_closed``, which fails OPEN because suppressing
a notification is cheap; reclaiming a workspace is not, so an unknown answer
must never authorise it. The escape is the explicit ``--allow-open-prs``
override, which is NOT ``--force`` — that one waives the unpushed-commit guard
and must not silently also disable this gate.

A worktree whose repo has no forge remote is CLEAR, not inconclusive: with no
forge there is no MR to protect.

LAYERING
--------
Policy only. Reading a PR's live state needs a concrete backend, which
``teatree.core`` may not import (``teatree.backends`` depends on core), so the
reader is INJECTED as :class:`PrStateReader` — the same split as
``pr_budget_gate`` (policy) and ``pr_budget_forge`` (forge access). The
interface layer supplies the concrete reader.
"""

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import PullRequest, Ticket, Worktree
from teatree.utils import git
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

_OVERRIDE_HINT = "Re-run with --allow-open-prs to reclaim anyway."


class OpenPullRequestTeardownError(RuntimeError):
    """Refusal raised when tearing a ticket down would reclaim a worktree behind an open PR."""


class PrStateReader(Protocol):
    """Reads one PR/MR's live state on the forge.

    Free to raise: the gate treats any exception as ``UNKNOWN`` and refuses, so
    an implementation never has to carry its own fail-closed policy.
    """

    def __call__(self, pr_url: str) -> PrOpenState: ...  # pragma: no branch


def open_pr_blockers(
    ticket: Ticket,
    worktrees: Sequence[Worktree],
    *,
    read_pr_state: PrStateReader,
) -> list[str]:
    """Reasons this ticket's worktrees must not be reclaimed.

    Empty means every PR/MR the gate can see is settled (merged or closed) and
    no branch under teardown backs an open one — the ticket is reclaimable.
    """
    if not worktrees:
        return []
    return _recorded_pr_blockers(ticket, read_pr_state) + _branch_pr_blockers(worktrees)


def check_no_open_prs(
    ticket: Ticket,
    worktrees: Sequence[Worktree],
    *,
    read_pr_state: PrStateReader,
    allow_open_prs: bool = False,
) -> None:
    """Raise :class:`OpenPullRequestTeardownError` unless *ticket* is reclaimable.

    *allow_open_prs* is the explicit operator override; it skips the forge reads
    entirely so an override never pays for a probe it would ignore.
    """
    if allow_open_prs:
        logger.warning(
            "open-PR teardown gate overridden for ticket %s — reclaiming despite any open PR",
            ticket.ticket_number or ticket.pk,
        )
        return

    blockers = open_pr_blockers(ticket, worktrees, read_pr_state=read_pr_state)
    if not blockers:
        return

    detail = "\n  ".join(blockers)
    msg = (
        f"Refusing to tear down ticket {ticket.ticket_number or ticket.pk} — it is not done:"
        f"\n  {detail}\n{_OVERRIDE_HINT}"
    )
    raise OpenPullRequestTeardownError(msg)


def _recorded_pr_blockers(ticket: Ticket, read_pr_state: PrStateReader) -> list[str]:
    """Blockers from the ticket's ``PullRequest`` rows, each verified live.

    ``MERGED`` is terminal and irreversible, so those rows are settled without a
    forge call. Every other row is re-read live: the FSM cannot express CLOSED,
    and its ``OPEN`` may simply be a state nobody advanced.
    """
    blockers: list[str] = []
    for row in PullRequest.objects.filter(ticket=ticket).exclude(state=PullRequest.State.MERGED).order_by("pk"):
        state = _live_pr_state(row.url, read_pr_state)
        if state == PrOpenState.OPEN:
            blockers.append(f"{row.repo} !{row.iid} is still OPEN on the forge: {row.url}")
        elif state == PrOpenState.UNKNOWN:
            blockers.append(f"could not read the forge state of {row.url} — refusing while it is unknown")
    return blockers


def _branch_pr_blockers(worktrees: Sequence[Worktree]) -> list[str]:
    """Blockers from asking the forge which branches under teardown back an open PR.

    The only source that sees an MR the model never recorded, and the one keyed
    on exactly what teardown destroys.
    """
    blockers: list[str] = []
    for worktree in worktrees:
        path = worktree.worktree_path
        if not path or not Path(path).is_dir():
            # Nothing on disk to reclaim for this row; the recorded-PR leg still
            # covers the ticket-level view.
            continue
        url = _open_pr_url_for_branch(Path(path), worktree.branch)
        if url is None:
            blockers.append(
                f"could not ask the forge whether {worktree.branch!r} backs an open PR "
                f"({worktree.repo_path}) — refusing while it is unknown"
            )
        elif url:
            blockers.append(f"branch {worktree.branch!r} ({worktree.repo_path}) backs an open PR: {url}")
    return blockers


def _live_pr_state(pr_url: str, read_pr_state: PrStateReader) -> PrOpenState:
    """*pr_url*'s live forge state, ``UNKNOWN`` when it cannot be read."""
    if not pr_url:
        return PrOpenState.UNKNOWN
    try:
        return read_pr_state(pr_url)
    except Exception:
        logger.warning("open-PR teardown gate could not read %s — failing closed", pr_url, exc_info=True)
        return PrOpenState.UNKNOWN


def _open_pr_url_for_branch(repo_dir: Path, branch: str) -> str | None:
    """The OPEN PR/MR URL backing *branch*, ``""`` when there is none, ``None`` when unknown.

    Deliberately not ``fast_push.ForgeClient.find_pr_url``: that primitive
    collapses "no PR" and "the probe failed" into ``""`` because its caller
    treats both as "create one", which is safe there. A reclaim gate needs the
    two apart — one clears the teardown, the other must block it.

    A repo whose ``origin`` is not a recognised forge returns ``""``: no forge,
    therefore no MR, therefore nothing to protect.
    """
    if not branch:
        return None
    remote = git.remote_url(repo=str(repo_dir))
    if "gitlab" in remote:
        return _first_url(["glab", "mr", "list", "--source-branch", branch, "-F", "json"], repo_dir, key="web_url")
    if "github" in remote:
        return _first_url(["gh", "pr", "list", "--head", branch, "--json", "url"], repo_dir, key="url")
    return ""


def _first_url(cmd: list[str], repo_dir: Path, *, key: str) -> str | None:
    """First ``key`` in the forge CLI's JSON array, ``""`` when empty, ``None`` when the probe failed.

    Both CLIs list only OPEN PRs/MRs by default, so a non-empty payload IS an
    open one. A missing binary raises ``FileNotFoundError`` rather than exiting
    non-zero, which is why the catch covers ``OSError`` as well as a bad exit.
    """
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, cwd=repo_dir)
    except OSError:
        logger.warning("open-PR teardown gate could not run %r in %s — failing closed", cmd[0], repo_dir)
        return None
    if result.returncode != 0:
        logger.warning("open-PR teardown gate probe %r failed in %s — failing closed", cmd[0], repo_dir)
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return str(payload[0].get(key, "")) if payload else ""
