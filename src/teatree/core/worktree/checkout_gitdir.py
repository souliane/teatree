"""Classify an on-disk checkout from the filesystem alone.

A linked worktree reaches its repository through the ABSOLUTE ``gitdir:`` pointer in
its ``.git`` file, so when that pointer names a path this venue cannot see — a host
``git worktree add`` tree bind-mounted into the container, whose clone lives only on
the host — every ``git`` query about the checkout fails exactly as it would for a
directory that was never a checkout. The two need opposite fixes (mount the clone vs.
stand somewhere else), so a refusal that cannot name which one it hit dead-ends.

Pure ``pathlib``, no ``git`` subprocess: the classification has to work precisely
where ``git`` cannot answer.
"""

from enum import Enum
from pathlib import Path

_GITDIR_PREFIX = "gitdir:"


class CheckoutKind(Enum):
    """What an on-disk path is, as far as adoption is concerned."""

    NOT_A_CHECKOUT = "not_a_checkout"
    MAIN_CLONE = "main_clone"
    LINKED_WORKTREE = "linked_worktree"
    UNREACHABLE_GITDIR = "unreachable_gitdir"


def gitdir_pointer(checkout: str | Path) -> Path | None:
    """The gitdir a linked worktree's ``.git`` file points at, or ``None``.

    ``None`` covers every non-linked-worktree shape (a clone's ``.git`` directory, a
    plain directory, a missing path) and a ``.git`` file that does not carry the
    pointer. Git normally writes an absolute path but supports a relative one
    (``--relative-paths``), which is resolved against the checkout.
    """
    marker = Path(checkout) / ".git"
    if not marker.is_file():
        return None
    try:
        first = marker.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first.startswith(_GITDIR_PREFIX):
        return None
    return _resolve_recorded(first[len(_GITDIR_PREFIX) :].strip(), Path(checkout))


def checkout_of_admin_entry(entry: Path) -> Path | None:
    """The checkout a clone's ``worktrees/<id>`` admin *entry* points back at, or ``None`` when unreadable.

    Its ``gitdir`` file names the checkout's ``.git``; under ``--relative-paths`` that name is
    relative to *entry*, so the raw text is not comparable to a checkout path.
    """
    try:
        recorded = (entry / "gitdir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return _resolve_recorded(recorded, entry).parent if recorded else None


def _resolve_recorded(recorded: str, base: Path) -> Path:
    pointer = Path(recorded)
    return pointer if pointer.is_absolute() else (base / pointer).resolve()


def classify_checkout(checkout: str | Path) -> CheckoutKind:
    """Which :class:`CheckoutKind` *checkout* is, using only the filesystem."""
    path = Path(checkout)
    if (path / ".git").is_dir():
        return CheckoutKind.MAIN_CLONE
    pointer = gitdir_pointer(path)
    if pointer is None:
        return CheckoutKind.NOT_A_CHECKOUT
    return CheckoutKind.LINKED_WORKTREE if pointer.is_dir() else CheckoutKind.UNREACHABLE_GITDIR
