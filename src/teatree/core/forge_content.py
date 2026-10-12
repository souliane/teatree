"""Conservative content-presence verdict for an already merged PR."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum


class ContentPresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ContentEvidence:
    presence: ContentPresence
    head_sha: str = ""
    default_sha: str = ""
    head_tree: str = ""
    default_tree: str = ""
    checked_paths: tuple[str, ...] = ()
    matching_paths: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ForgeTree:
    sha: str
    tree: str
    blobs: Mapping[str, str | None]


def assess_content(
    *,
    head: ForgeTree,
    default: ForgeTree,
    changed_paths: list[str],
) -> ContentEvidence:
    """Presence is proven by full-tree equality or any changed-path blob equality."""
    paths = tuple(dict.fromkeys(changed_paths))
    base = ContentEvidence(
        ContentPresence.UNKNOWN,
        head_sha=head.sha,
        default_sha=default.sha,
        head_tree=head.tree,
        default_tree=default.tree,
        checked_paths=paths,
    )
    if not all((head.sha, default.sha, head.tree, default.tree)) or not paths:
        return replace(base, reason="missing head, default, tree, or changed paths")
    if set(paths) - head.blobs.keys() or set(paths) - default.blobs.keys():
        return replace(base, reason="incomplete blob read")
    if head.tree == default.tree:
        return replace(base, presence=ContentPresence.PRESENT, reason="tree equality")
    matches = tuple(path for path in paths if head.blobs[path] == default.blobs[path])
    if matches:
        return replace(
            base, presence=ContentPresence.PRESENT, matching_paths=matches, reason="changed-path blob equality"
        )
    return replace(base, presence=ContentPresence.ABSENT, reason="all changed-path blobs differ")
