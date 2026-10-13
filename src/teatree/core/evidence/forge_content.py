"""Whether a merged PR's content reached the default branch — the forge facts and the pure verdict."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ContentVerdictKind(StrEnum):
    ABSENT = "absent"
    PRESENT_AT_TIP = "present_at_tip"
    CARRIED_BY_MERGE = "carried_by_merge"
    NOT_MERGED = "not_merged"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PrContentSnapshot:
    """Path -> object-sha maps per commit; a path missing from a map is absent at that commit.

    ``merge_commit_on_default`` is ``False`` for a PR whose head equals its base: that
    merge commit IS the base, so its place in the default history proves nothing.
    """

    merged: bool = False
    merge_commit: str = ""
    content_head: str = ""
    merge_base: str = ""
    default_branch: str = ""
    default_tip: str = ""
    content_head_on_default: bool = False
    merge_commit_on_default: bool = False
    base_blobs: Mapping[str, str] = field(default_factory=dict)
    head_blobs: Mapping[str, str] = field(default_factory=dict)
    merge_blobs: Mapping[str, str] = field(default_factory=dict)
    tip_blobs: Mapping[str, str] = field(default_factory=dict)
    unreadable: str = ""


@dataclass(frozen=True, slots=True)
class ContentVerdict:
    kind: ContentVerdictKind
    changed: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    reason: str = ""


def classify_content(snapshot: PrContentSnapshot) -> ContentVerdict:
    if snapshot.unreadable:
        return ContentVerdict(ContentVerdictKind.UNKNOWN, reason=snapshot.unreadable)
    if not snapshot.merged:
        return ContentVerdict(ContentVerdictKind.NOT_MERGED, reason="the forge does not report the PR merged")
    return _classify_merged(snapshot)


def _classify_merged(snapshot: PrContentSnapshot) -> ContentVerdict:
    """Any one path of the PR found at the merge commit or the tip refuses: partial presence is presence."""
    base, head = snapshot.base_blobs, snapshot.head_blobs
    changed = tuple(sorted(path for path in base.keys() | head.keys() if base.get(path) != head.get(path)))
    if not changed:
        return ContentVerdict(ContentVerdictKind.UNKNOWN, reason="no diff between the merge base and the content head")
    carried = tuple(path for path in changed if snapshot.merge_blobs.get(path) == head.get(path))
    present = tuple(path for path in changed if snapshot.tip_blobs.get(path) == head.get(path))
    rules = (
        (
            snapshot.content_head_on_default,
            ContentVerdictKind.PRESENT_AT_TIP,
            "the content head is in the default history",
        ),
        (
            snapshot.merge_commit_on_default,
            ContentVerdictKind.CARRIED_BY_MERGE,
            "the merge commit is in the default history",
        ),
        (
            bool(carried),
            ContentVerdictKind.CARRIED_BY_MERGE,
            f"{len(carried)} of {len(changed)} paths match the merge commit",
        ),
        (
            bool(present),
            ContentVerdictKind.PRESENT_AT_TIP,
            f"{len(present)} of {len(changed)} paths match the default tip",
        ),
        (True, ContentVerdictKind.ABSENT, f"none of {len(changed)} paths reached the default branch"),
    )
    kind, reason = next((kind, reason) for hit, kind, reason in rules if hit)
    absent = tuple(path for path in changed if path not in carried and path not in present)
    return ContentVerdict(kind, changed=changed, absent=absent, reason=reason)
