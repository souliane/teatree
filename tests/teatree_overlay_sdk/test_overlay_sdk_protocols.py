"""The exported protocols freeze exactly the read surface the concrete models expose (#3342).

Gives ``test_overlay_sdk_surface`` real teeth on the *parameter type* a frozen
hook passes: renaming a contractual field on ``Worktree`` / ``Ticket`` (or
dropping it) fails HERE, in core's CI, immediately — instead of asynchronously in
a consumer overlay that hand-rolled the same protocol and type-checks green until
runtime.
"""

from teatree.core.models import Ticket, Worktree
from teatree.overlay_sdk import TicketLike, WorktreeLike
from teatree.overlay_sdk.protocols import TicketLike as TicketLikeDirect
from teatree.overlay_sdk.protocols import WorktreeLike as WorktreeLikeDirect


def _protocol_members(protocol: type) -> set[str]:
    return {name for name in vars(protocol) if not name.startswith("_")}


def test_exported_protocols_are_the_module_protocols() -> None:
    assert WorktreeLike is WorktreeLikeDirect
    assert TicketLike is TicketLikeDirect


def test_worktree_model_exposes_every_worktreelike_field() -> None:
    for name in ("repo_path", "branch", "db_name", "worktree_path", "extra", "ticket"):
        assert hasattr(Worktree, name), f"Worktree lost the contractual field {name!r}"


def test_ticket_model_exposes_every_ticketlike_field() -> None:
    for name in ("issue_url", "overlay", "ticket_number"):
        assert hasattr(Ticket, name), f"Ticket lost the contractual field {name!r}"


def test_protocols_are_read_only() -> None:
    # Every contractual member is a read-only ``@property`` stub, never a mutable
    # attribute — a hook reads the worktree, it never mutates it.
    for protocol in (WorktreeLike, TicketLike):
        for name in _protocol_members(protocol):
            assert isinstance(getattr(protocol, name), property), f"{protocol.__name__}.{name} must be a property"
