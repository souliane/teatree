"""Is this directory a live checkout? — a question one execution context cannot answer alone.

A linked checkout records its admin dir as an ABSOLUTE path inside its own
``.git`` file, and that path is written by whichever context created the
checkout. Read from a context that reaches the clone somewhere else — or after
the clone is moved — the recorded path resolves to nothing, and ``git`` answers
``fatal: not a git repository`` in the same words it uses for a directory that
never held a repository at all. A probe that reads that as proof of death cannot
tell a live checkout from a dead one, yet the remediation it authorises is a
deletion.

This module supplies the discriminator, in two halves.

:func:`claims_to_be_a_checkout` separates "there is nothing here that ever
claimed to be a checkout" — the one thing a single context CAN prove — from "the
admin dir this checkout names is absent HERE", which proves only that this
context cannot follow the pointer.

:func:`admin_entry_for` closes the other half: given a clone this context CAN
see, the admin entry is looked up by NAME rather than by the recorded absolute
path, so a checkout whose pointer is merely scoped to another context is proven
live instead of presumed dead. The name is context-free — two contexts reaching
one clone disagree about its path, never about which entries it holds.
"""

from dataclasses import dataclass
from pathlib import Path

_GITDIR_PREFIX = "gitdir:"
_ADMIN_ENTRIES = ("worktrees",)


@dataclass(frozen=True, slots=True)
class GitdirPointer:
    """The admin dir a checkout's ``.git`` file names, exactly as its creator wrote it."""

    target: Path

    @property
    def entry_name(self) -> str:
        """The admin entry's name under ``<clone>/.git/worktrees/``.

        The only part of the recorded pointer that survives a change of context:
        the prefix is whatever path the creator reached the clone by, the name is
        the clone's own identifier for this checkout.
        """
        return self.target.name

    @property
    def resolves_here(self) -> bool:
        return self.target.is_dir()


def claims_to_be_a_checkout(path: Path) -> bool:
    """Does *path* carry a ``.git`` entry at all?

    The single positive proof available without a clone. A directory carrying no
    ``.git`` never claimed to be a checkout, so git's refusal there is about the
    DIRECTORY. A directory that carries one has staked the claim, and git's
    refusal is then about a pointer, which this context may simply be unable to
    follow.
    """
    return (path / ".git").exists()


def read_gitdir_pointer(checkout: Path) -> GitdirPointer | None:
    """The ``gitdir:`` target recorded in *checkout*'s ``.git`` file, or ``None``.

    ``None`` covers every shape that records no pointer: no ``.git`` at all, a
    ``.git`` DIRECTORY (an ordinary repo root, which names nothing elsewhere), an
    unreadable file, and a file whose content is not a gitfile.
    """
    gitfile = checkout / ".git"
    try:
        if not gitfile.is_file():
            return None
        content = gitfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    first = content.strip().splitlines()
    if not first or not first[0].startswith(_GITDIR_PREFIX):
        return None
    recorded = first[0].removeprefix(_GITDIR_PREFIX).strip()
    return GitdirPointer(Path(recorded)) if recorded else None


def context_scoped_pointer(checkout: Path) -> GitdirPointer | None:
    """*checkout*'s recorded admin dir when this context cannot see it; ``None`` otherwise.

    A non-``None`` answer is the shape that reads as death and is not: the
    checkout named an admin dir, and the naming is sound in whatever context
    wrote it.
    """
    pointer = read_gitdir_pointer(checkout)
    return None if pointer is None or pointer.resolves_here else pointer


def admin_entry_for(checkout: Path, clone: Path) -> Path | None:
    """*clone*'s admin entry for *checkout*, when this context can see one.

    Proof of LIFE, resolved from the clone rather than from the checkout's own
    unusable pointer. Two conditions, both required: the clone holds an entry
    under the name the checkout recorded, and that entry names a checkout
    directory of this checkout's own name. The second is what stops a same-named
    entry belonging to some other checkout from vouching for this one — the
    entry's own recorded path is context-scoped too, so only its tail is
    comparable.
    """
    pointer = read_gitdir_pointer(checkout)
    if pointer is None:
        return None
    entry = clone.joinpath(".git", *_ADMIN_ENTRIES, pointer.entry_name)
    recorded = _checkout_named_by(entry)
    if recorded is None or recorded.parent.name != checkout.name:
        return None
    return entry


def _checkout_named_by(entry: Path) -> Path | None:
    """The checkout path an admin *entry* points back at, as the creating context wrote it."""
    try:
        recorded = (entry / "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(recorded) if recorded else None


__all__ = [
    "GitdirPointer",
    "admin_entry_for",
    "claims_to_be_a_checkout",
    "context_scoped_pointer",
    "read_gitdir_pointer",
]
