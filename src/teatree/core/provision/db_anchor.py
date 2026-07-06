"""Cross-DB guard for the lifecycle/ship path (#779).

Each git worktree gets an isolated control DB (``teatree.paths``). The
installed ``t3`` and the live loop always proxy through the main clone, so
``t3 <ov> lifecycle visit-phase`` and ``t3 <ov> pr create`` operate on the
single *true canonical* DB. But ``uv run manage.py lifecycle visit-phase``
invoked from inside a worktree resolves to that worktree's **isolated** DB.

The defect is symmetric: a maker's ``testing``/``retro`` recorded from a
worktree, and a reviewer's ``reviewing`` recorded from a worktree, both land
in an isolated DB the shipping gate (which reads the true canonical DB) never
consults — producing a verbatim ``missing: [...]`` block whose phases were in
fact recorded, just in the wrong DB. This cost multiple multi-cycle
misdiagnoses (#764, #628, #769, #777, #778).

This module makes that condition LOUD instead of silent: a ticket-bound
lifecycle/ship command running against a worktree-isolated DB refuses with an
actionable error naming the true canonical DB and the correct ``t3`` command,
so every phase visit is routed through the one DB the gate consults. It is
the same doctrine as ``paths.CanonicalDBFromWorktreeError`` (worktree code
refusing to touch the canonical DB), applied to the phase-attestation path.
"""

from pathlib import Path

from django.db import connection

from teatree import paths
from teatree.core.models import Ticket


class WrongWorktreeDBError(RuntimeError):
    """Raised when a lifecycle/ship command targets a worktree-isolated DB.

    Phase visits and the shipping gate must agree on a single DB. The
    installed ``t3``/loop always use the true canonical DB; a command run via
    ``uv run manage.py`` from a worktree would silently use that worktree's
    isolated DB instead. Refusing here — and naming the canonical DB and the
    correct command — prevents the recurring "recorded but gate can't see it"
    misdiagnosis.
    """

    def __init__(self, ticket: Ticket, *, active_db: Path, canonical_db: Path, worktree_path: str) -> None:
        wt = worktree_path or "<unknown>"
        message = (
            f"Refusing to run a lifecycle/ship operation for ticket {ticket.pk} "
            f"against the worktree-isolated control DB:\n"
            f"  active (isolated) DB : {active_db}\n"
            f"  ticket worktree      : {wt}\n"
            f"  canonical DB (gate)  : {canonical_db}\n"
            f"Phase visits recorded here are invisible to the shipping gate, "
            f"which reads the canonical DB. Re-run via the global `t3` proxy so "
            f"the write lands in the canonical DB, e.g.:\n"
            f'  t3 <overlay> lifecycle visit-phase {ticket.pk} <phase> --agent-id "..."\n'
            f"  t3 <overlay> pr create {ticket.pk}\n"
            f"Do not work around this by pointing XDG_DATA_HOME at the canonical "
            f"dir — fix the invocation."
        )
        super().__init__(message)
        self.ticket = ticket
        self.active_db = active_db
        self.canonical_db = canonical_db


def _worktree_path_for(ticket: Ticket) -> str:
    # Stable ordering: a ticket can have several worktrees (multi-repo);
    # the refusal message must name a deterministic one across invocations.
    worktree = ticket.worktrees.order_by("pk").first()  # ty: ignore[unresolved-attribute]
    if worktree is None:
        return ""
    return worktree.worktree_path


def _active_db_path() -> str:
    """The DB file the *live Django connection* is bound to.

    This is the only DB this process actually reads/writes — what matters for
    the gate/visit split, not the static code-location flag. Under the test
    runner this is an in-memory / ``file::memory:`` database, never a real
    per-worktree file, so the guard is inert in tests by construction.
    """
    return str(connection.settings_dict.get("NAME", ""))


def _is_worktree_isolated_db(db_path: str, *, isolation_root: Path) -> bool:
    """True iff *db_path* is a real ``db.sqlite3`` file under the isolation root.

    ``:memory:`` / ``file::memory:`` test databases and the true canonical DB
    are excluded — only an on-disk per-worktree isolated control DB (the
    #779 cross-DB condition) trips this.
    """
    if not db_path or ":memory:" in db_path:
        return False
    try:
        Path(db_path).resolve().relative_to(isolation_root.resolve())
    except ValueError:
        return False
    return True


def assert_lifecycle_db_is_canonical(
    ticket: Ticket,
    *,
    auto_isolated: bool | None = None,
    active_db: Path | None = None,
    canonical_db: Path | None = None,
) -> None:
    """Refuse a ticket-bound lifecycle/ship op running on a worktree DB (#779).

    The trip condition is the *live Django connection* being bound to a real
    per-worktree isolated ``db.sqlite3`` (under
    :func:`paths.auto_isolated_worktrees_dir`) — exactly the
    ``uv run manage.py`` -from-a-worktree case whose writes/reads never reach
    the canonical DB the shipping gate consults. ``t3 <ov>`` proxies through
    the main clone (canonical DB) and never trips; ``:memory:`` test DBs are
    never under the isolation root so the guard is inert in tests without a
    test-only branch.

    ``auto_isolated`` lets tests force the decision deterministically without
    rebinding a real sqlite connection; when omitted the live connection's DB
    path is classified. ``active_db`` / ``canonical_db`` override the names
    embedded in the refusal message (production reads the real connection /
    :data:`paths.TRUE_CANONICAL_DB`).
    """
    if auto_isolated is None:
        is_isolated = _is_worktree_isolated_db(
            _active_db_path(),
            isolation_root=paths.auto_isolated_worktrees_dir(),
        )
    else:
        is_isolated = auto_isolated
    if not is_isolated:
        return
    raise WrongWorktreeDBError(
        ticket,
        active_db=Path(_active_db_path()) if active_db is None else active_db,
        canonical_db=paths.TRUE_CANONICAL_DB if canonical_db is None else canonical_db,
        worktree_path=_worktree_path_for(ticket),
    )
