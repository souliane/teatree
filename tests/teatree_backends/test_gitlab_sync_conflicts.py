"""GitLab conflict-signal detection for the followup sweep.

``is_conflicted`` reads the three signals the MR list payload exposes and
must never cry wolf on a still-computing (``unchecked``) or clean state.
``collect_conflicted_mrs`` translates only the conflicted raw MRs into the
overlay-agnostic ``ConflictedMR`` shape.
"""

from teatree.backends.gitlab.sync_conflicts import collect_conflicted_mrs, is_conflicted
from teatree.types import RawAPIDict, SyncResult


class TestIsConflicted:
    def test_has_conflicts_flag(self) -> None:
        assert is_conflicted({"has_conflicts": True}) is True

    def test_deprecated_merge_status(self) -> None:
        assert is_conflicted({"merge_status": "cannot_be_merged"}) is True

    def test_detailed_merge_status(self) -> None:
        assert is_conflicted({"detailed_merge_status": "conflict"}) is True

    def test_clean_can_be_merged(self) -> None:
        assert is_conflicted({"has_conflicts": False, "merge_status": "can_be_merged"}) is False

    def test_unchecked_is_not_a_conflict(self) -> None:
        # Still-computing mergeability must not raise a false alarm.
        assert is_conflicted({"merge_status": "unchecked", "detailed_merge_status": "checking"}) is False

    def test_non_conflict_detailed_status_is_not_conflict(self) -> None:
        # ci_must_pass / not_approved / broken_status are NOT merge conflicts.
        assert is_conflicted({"detailed_merge_status": "ci_must_pass"}) is False

    def test_empty_payload_is_not_a_conflict(self) -> None:
        assert is_conflicted({}) is False


class TestCollectConflictedMrs:
    def test_collects_only_conflicted_with_repo_short_name(self) -> None:
        raw: list[RawAPIDict] = [
            {
                "iid": 7649,
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/7649",
                "title": "feat: conflicting",
                "has_conflicts": True,
                "references": {"full": "org/repo!7649"},
            },
            {
                "iid": 7700,
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/7700",
                "title": "feat: clean",
                "has_conflicts": False,
            },
        ]
        result = SyncResult()

        collect_conflicted_mrs(raw, result)

        assert len(result.conflicted_mrs) == 1
        conflicted = result.conflicted_mrs[0]
        assert conflicted.iid == 7649
        assert conflicted.repo == "repo"
        assert conflicted.title == "feat: conflicting"
        assert conflicted.to_dict() == {
            "iid": 7649,
            "repo": "repo",
            "web_url": "https://gitlab.com/org/repo/-/merge_requests/7649",
            "title": "feat: conflicting",
        }

    def test_empty_input_leaves_result_untouched(self) -> None:
        result = SyncResult()
        collect_conflicted_mrs([], result)
        assert result.conflicted_mrs == []
