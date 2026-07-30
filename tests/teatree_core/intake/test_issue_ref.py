"""``canonicalize_issue_ref`` — the ``workspace ticket`` non-URL input guard.

The recorded bug: ``workspace ticket 3274`` stored ``issue_url='3274'`` verbatim,
which resolved the overlay to ``''`` and produced malformed duplicate tickets. A
bare number must canonicalize to the overlay's full issue URL or be rejected — a
malformed ``issue_url`` must be impossible.
"""

import pytest

from teatree.core.intake.issue_ref import InvalidIssueRefError, canonicalize_issue_ref


class _FakeOverlay:
    def __init__(self, resolved: str | None) -> None:
        self._resolved = resolved
        self.asked: list[int] = []

    def resolve_issue_token(self, number: int) -> str | None:
        self.asked.append(number)
        return self._resolved


class TestBareNumberCanonicalization:
    def test_bare_number_resolves_to_full_url(self) -> None:
        overlay = _FakeOverlay("https://github.com/souliane/teatree/issues/3274")
        assert canonicalize_issue_ref(overlay, "3274") == "https://github.com/souliane/teatree/issues/3274"
        assert overlay.asked == [3274]

    def test_hash_prefixed_number_resolves(self) -> None:
        overlay = _FakeOverlay("https://github.com/souliane/teatree/issues/38")
        assert canonicalize_issue_ref(overlay, "#38") == "https://github.com/souliane/teatree/issues/38"
        assert overlay.asked == [38]

    def test_surrounding_whitespace_is_stripped(self) -> None:
        overlay = _FakeOverlay("https://github.com/souliane/teatree/issues/7")
        assert canonicalize_issue_ref(overlay, "  7  ") == "https://github.com/souliane/teatree/issues/7"


class TestRejection:
    def test_unresolvable_bare_number_is_refused(self) -> None:
        overlay = _FakeOverlay(None)
        with pytest.raises(InvalidIssueRefError) as exc:
            canonicalize_issue_ref(overlay, "3274")
        assert "3274" in str(exc.value)

    def test_empty_argument_is_refused(self) -> None:
        overlay = _FakeOverlay("https://github.com/souliane/teatree/issues/1")
        with pytest.raises(InvalidIssueRefError):
            canonicalize_issue_ref(overlay, "   ")
        assert overlay.asked == []


class TestPassthrough:
    def test_full_url_passes_through_unresolved(self) -> None:
        overlay = _FakeOverlay(None)
        url = "https://github.com/souliane/teatree/issues/42"
        assert canonicalize_issue_ref(overlay, url) == url
        assert overlay.asked == []

    def test_owner_repo_slug_passes_through(self) -> None:
        overlay = _FakeOverlay(None)
        assert canonicalize_issue_ref(overlay, "souliane/teatree#42") == "souliane/teatree#42"
        assert overlay.asked == []

    def test_non_numeric_token_passes_through(self) -> None:
        overlay = _FakeOverlay(None)
        assert canonicalize_issue_ref(overlay, "PROJ-1") == "PROJ-1"
        assert overlay.asked == []
