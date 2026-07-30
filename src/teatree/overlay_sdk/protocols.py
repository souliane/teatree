"""Read-only structural protocols for the objects frozen hook signatures pass (#3342).

The provisioning/runtime facet hooks on :class:`~teatree.core.overlay.OverlayBase`
(and its facets) receive a :class:`~teatree.core.models.Worktree`. That ORM model
is deliberately NOT in ``overlay_sdk.__all__`` — per #3157 AH-9 it must stay free
to evolve. But an overlay overriding a hook still needs a *name* to annotate the
parameter with, and both available answers are poor: importing the ORM model
couples the overlay's type annotations to an unfrozen internal, and hand-rolling a
``Protocol`` is a private guess that drifts silently from the real model.

These protocols close that gap: an overlay annotates its override with
``WorktreeLike`` — a valid WIDENING of the concrete parameter the base hook
declares, so no override is forced to migrate — and takes a typing dependency on
a FROZEN, exported name instead of the unfrozen ORM model or a private guess.

They freeze exactly the read surface the hook contract already obliges an overlay
to read — a subset core effectively promised by passing the object into a frozen
signature — NOT the model, its manager, its migrations, or its behaviour.
``teatree.core.models.Worktree`` / ``teatree.core.models.Ticket`` stay entirely
free to evolve; only the named, read-only fields below are contractual, and a
change to one fails :mod:`tests.teatree_overlay_sdk.test_protocols` in *core's*
CI — the drift guard the hand-rolled-Protocol approach never had.

Read-only by design (``@property``, never mutable attributes): hooks read the
worktree, they do not mutate it, and a protocol that permitted writes would
over-promise. The concrete ORM models structurally satisfy these protocols, so
core's own call sites are unchanged. Growing the set is a deliberate act, the
same rule the ``overlay_sdk`` surface states.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

__all__ = ["TicketLike", "WorktreeLike"]


@runtime_checkable
class TicketLike(Protocol):
    """The read-only ticket surface a provisioning hook legitimately reads.

    Structurally satisfied by :class:`teatree.core.models.Ticket`. Reached from
    :attr:`WorktreeLike.ticket`.
    """

    @property
    def issue_url(self) -> str: ...

    @property
    def overlay(self) -> str: ...

    @property
    def ticket_number(self) -> str: ...


@runtime_checkable
class WorktreeLike(Protocol):
    """The read-only worktree surface the frozen facet hooks pass.

    Structurally satisfied by :class:`teatree.core.models.Worktree`. ``repo_path``
    is the repo IDENTIFIER (e.g. ``owner/repo``), not a filesystem path — the
    on-disk path is :attr:`worktree_path` (derived from ``extra['worktree_path']``).
    """

    @property
    def repo_path(self) -> str: ...

    @property
    def branch(self) -> str: ...

    @property
    def db_name(self) -> str: ...

    @property
    def worktree_path(self) -> str: ...

    @property
    def extra(self) -> Mapping[str, object] | None: ...

    @property
    def ticket(self) -> TicketLike | None: ...
