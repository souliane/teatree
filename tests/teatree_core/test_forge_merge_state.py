from teatree.core.forge_merge_state import MergeConflictState, PrMergeState


def test_merge_state_defaults_keep_an_unreadable_state_unknown() -> None:
    state = PrMergeState(state="", merge_commit_oid="")
    assert state.conflict is MergeConflictState.UNKNOWN
    assert (state.merged_by, state.merged_at, state.is_merged) == ("", "", False)


def test_merged_state_accepts_case_insensitive_forge_spelling() -> None:
    assert PrMergeState(state="merged", merge_commit_oid="a" * 40).is_merged
