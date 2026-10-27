from dataclasses import dataclass
from enum import StrEnum


class MergeConflictState(StrEnum):
    CONFLICTED = "conflicted"
    CLEAN = "clean"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PrMergeState:
    state: str
    merge_commit_oid: str
    conflict: MergeConflictState = MergeConflictState.UNKNOWN
    merged_by: str = ""
    merged_at: str = ""

    @property
    def is_merged(self) -> bool:
        return self.state.upper() == "MERGED"
