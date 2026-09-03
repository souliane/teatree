"""Ground a consolidated rule's ``durable_destination`` against the core checkout (#2663).

Pass 1 grounds a cluster's CITATIONS against the extract's snippets
(:func:`teatree.loops.dream.engine.check_grounding`). The ``durable_destination`` —
the field Pass-2 triage reads to decide whether a row becomes a scheduled fix — had
no grounding at all: a prefix match on the raw string. Both directions were wrong.
An invented module under a real package matched a prefix and scheduled a coding
task for a file that does not exist; a real core path outside the
six-entry prefix list (``evals/``, ``hooks/``, ``tests/``, ``AGENTS.md``) did not
match and the gap was silently kept as memory.

So core-ness is tree containment, not a prefix: a destination is a core fix when it
resolves inside the core checkout — an existing path, or a new file in an existing
sub-directory (the fix for a gap is often a module that does not exist yet). The
resolution is case-insensitive per component, because the distiller emits sloppy
destinations and the pre-#2663 predicate lowercased the whole string.

The prefix tuple survives ONLY for the case where there is no tree to read (a
non-editable install): degrading to "nothing is a core fix" would silently kill the
whole core-gap pipeline, so an unverifiable checkout keeps the old answer and says so.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from teatree.paths import PathHelpers

logger = logging.getLogger(__name__)

#: The pre-#2663 predicate, kept as the answer for a checkout that cannot be read.
_CORE_DESTINATION_PREFIXES = ("skills/", "src/teatree", "teatree/", "scripts/", "blueprint", "agents/")


@dataclass(frozen=True, slots=True)
class DestinationVerdict:
    """One ``durable_destination`` judged against the core checkout.

    ``rel_path`` is the destination canonicalised to the tree's own spelling (so a
    mis-cased or absolute-but-inside form reads the same downstream). ``verifiable``
    is False when no core checkout resolved, in which case ``in_core_tree`` carries
    the legacy prefix answer rather than a grounded one.
    """

    rel_path: str
    in_core_tree: bool
    verifiable: bool
    reason: str


def points_at_core_fix(destination: str, *, root: Path | None = None) -> bool:
    """Whether *destination* names a teatree-core fix path.

    The single classifier behind Pass-2 triage
    (:func:`teatree.loops.dream.promote_memory.triage_disposition`), the promotion
    chokepoint, and the compliance recurrence redirect
    (:func:`teatree.loops.dream.compliance._is_memory_only`).
    """
    return classify_destination(destination, root=root).in_core_tree


def classify_destination(destination: str, *, root: Path | None = None) -> DestinationVerdict:
    """Judge *destination* against the core checkout, naming WHY when it is ungrounded."""
    home = destination.strip()
    if not home:
        return DestinationVerdict(rel_path="", in_core_tree=False, verifiable=True, reason="it names no destination")

    tree = root if root is not None else PathHelpers.core_repo_root()
    if tree is None:
        matched = home.lower().startswith(_CORE_DESTINATION_PREFIXES)
        logger.warning(
            "dream: no core checkout resolved — falling back to the prefix predicate for %r (core=%s).", home, matched
        )
        return DestinationVerdict(
            rel_path=home, in_core_tree=matched, verifiable=False, reason="no core checkout resolved"
        )

    relative = _repo_relative(home, tree)
    if relative is None:
        return DestinationVerdict(
            rel_path=home, in_core_tree=False, verifiable=True, reason=f"{home!r} lies outside the core tree"
        )
    return _resolve_in_tree(relative, tree)


def _repo_relative(home: str, tree: Path) -> PurePosixPath | None:
    """*home* as a path relative to *tree*, or ``None`` when it cannot name one.

    An absolute destination is legitimate on both sides of the split — a memory file
    carries the memory dir's absolute path, while a core one may carry the checkout's
    — so it is rewritten relative when it is inside the tree rather than refused.
    """
    candidate = PurePosixPath(home.replace(os.sep, "/"))
    if candidate.is_absolute():
        try:
            candidate = PurePosixPath(Path(home).resolve().relative_to(tree.resolve()))
        except ValueError:
            return None
    if not candidate.parts or any(part == ".." for part in candidate.parts):
        return None
    return candidate


def _resolve_in_tree(relative: PurePosixPath, tree: Path) -> DestinationVerdict:
    resolved = _walk_case_insensitively(relative, tree)
    if isinstance(resolved, str):
        return DestinationVerdict(rel_path=str(relative), in_core_tree=False, verifiable=True, reason=resolved)

    rel_path = str(resolved.relative_to(tree))
    if not _contained(resolved, tree):
        return DestinationVerdict(
            rel_path=rel_path,
            in_core_tree=False,
            verifiable=True,
            reason=f"{rel_path!r} resolves outside the core tree",
        )
    if resolved.exists():
        return DestinationVerdict(rel_path=rel_path, in_core_tree=True, verifiable=True, reason="")
    # The fix for a gap is often a module that does not exist yet, so an absent leaf
    # inside a real package still names a core fix — an absent PACKAGE does not.
    parent = resolved.parent
    if parent != tree and parent.is_dir() and _contained(parent, tree):
        return DestinationVerdict(rel_path=rel_path, in_core_tree=True, verifiable=True, reason="")
    missing = str(parent.relative_to(tree)) if parent != tree else rel_path
    return DestinationVerdict(
        rel_path=rel_path, in_core_tree=False, verifiable=True, reason=f"no {missing!r} in the core tree"
    )


def _walk_case_insensitively(relative: PurePosixPath, tree: Path) -> Path | str:
    """*relative* resolved against *tree*'s own spelling, or the reason it could not be.

    An exact hit always wins; a single case-insensitive hit is accepted, and two are
    refused rather than guessed. Components past the deepest existing directory are
    appended verbatim, which is what lets a not-yet-created file be judged.
    """
    current = tree
    for index, part in enumerate(relative.parts):
        if (current / part).exists():
            current /= part
            continue
        matches = _case_insensitive_matches(current, part)
        if len(matches) > 1:
            return f"{str(relative)!r} is ambiguous: {part!r} matches {sorted(matches)} case-insensitively"
        if not matches:
            return current / PurePosixPath(*relative.parts[index:])
        current /= matches[0]
    return current


def _case_insensitive_matches(directory: Path, part: str) -> list[str]:
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    return [entry.name for entry in entries if entry.name.lower() == part.lower()]


def _contained(path: Path, tree: Path) -> bool:
    """Whether *path* stays inside *tree* once symlinks are followed."""
    try:
        return path.resolve().is_relative_to(tree.resolve())
    except OSError:
        return False


__all__ = ["DestinationVerdict", "classify_destination", "points_at_core_fix"]
