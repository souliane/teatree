"""Stay-inline-once-inline review-shape gate (PR-08, item 4).

A fake GitLab API returns a canned ``draft_notes`` list so the gate's
network-touching read is exercised without a real forge.
"""

from teatree.cli.review.inline_shape_gate import check_inline_shape, count_inline_drafts


class _FakeAPI:
    """Minimal stand-in exposing ``get_json`` for the draft-notes endpoint."""

    def __init__(self, notes: object, *, raises: bool = False) -> None:
        self._notes = notes
        self._raises = raises

    def get_json(self, path: str) -> object:
        if self._raises:
            msg = "boom"
            raise RuntimeError(msg)
        return self._notes


_INLINE_DRAFT = {"id": 1, "note": "fix", "position": {"new_path": "a.py", "new_line": 10}}
_MR_LEVEL_DRAFT = {"id": 2, "note": "summary", "position": None}


class TestCountInlineDrafts:
    def test_counts_only_inline(self) -> None:
        api = _FakeAPI([_INLINE_DRAFT, _MR_LEVEL_DRAFT, _INLINE_DRAFT])
        assert count_inline_drafts(api, "org%2Frepo", 4) == 2

    def test_empty_list(self) -> None:
        assert count_inline_drafts(_FakeAPI([]), "org%2Frepo", 4) == 0

    def test_fetch_failure_returns_zero(self) -> None:
        assert count_inline_drafts(_FakeAPI(None, raises=True), "org%2Frepo", 4) == 0


class TestCheckInlineShape:
    def test_refuses_mr_level_when_inline_drafts_exist(self) -> None:
        api = _FakeAPI([_INLINE_DRAFT])
        refusal = check_inline_shape(api=api, encoded_repo="org%2Frepo", mr=4, inline=False)
        assert "already has 1 inline draft" in refusal

    def test_allows_when_no_inline_drafts(self) -> None:
        api = _FakeAPI([_MR_LEVEL_DRAFT])
        assert check_inline_shape(api=api, encoded_repo="org%2Frepo", mr=4, inline=False) == ""

    def test_inline_post_is_never_blocked(self) -> None:
        api = _FakeAPI([_INLINE_DRAFT])
        assert check_inline_shape(api=api, encoded_repo="org%2Frepo", mr=4, inline=True) == ""

    def test_force_general_escape(self) -> None:
        api = _FakeAPI([_INLINE_DRAFT])
        assert check_inline_shape(api=api, encoded_repo="org%2Frepo", mr=4, inline=False, force_general=True) == ""

    def test_fetch_failure_fails_open(self) -> None:
        api = _FakeAPI(None, raises=True)
        assert check_inline_shape(api=api, encoded_repo="org%2Frepo", mr=4, inline=False) == ""
