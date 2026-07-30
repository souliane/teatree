"""The SHA-bind predicate: clearance binds to the exact reviewed tree (§17.4.3 step 2)."""

from teatree.core.merge.sha_bind import verify_sha_bound

_SHA_A = "a" * 40
_SHA_B = "b" * 40


class TestVerifyShaBound:
    def test_matched_sha_is_bound(self) -> None:
        assert verify_sha_bound(_SHA_A, _SHA_A) is True

    def test_moved_sha_is_not_bound(self) -> None:
        assert verify_sha_bound(_SHA_A, _SHA_B) is False

    def test_comparison_is_case_insensitive(self) -> None:
        assert verify_sha_bound(_SHA_A.upper(), _SHA_A) is True

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert verify_sha_bound(f"  {_SHA_A}  ", _SHA_A) is True

    def test_empty_cleared_is_never_bound(self) -> None:
        assert verify_sha_bound("", _SHA_A) is False

    def test_empty_live_is_never_bound(self) -> None:
        assert verify_sha_bound(_SHA_A, "") is False
