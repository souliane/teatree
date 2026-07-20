"""Behaviour of the worktree-teardown result value object."""

from teatree.core.cleanup.cleanup import CleanupResult as ReExported
from teatree.core.cleanup.cleanup_result import CleanupResult


class TestCleanupResult:
    def test_clean_when_no_errors(self) -> None:
        result = CleanupResult(label="Cleaned: repo (branch)")
        assert result.clean is True
        assert str(result) == "Cleaned: repo (branch)"

    def test_dirty_when_errors_present(self) -> None:
        result = CleanupResult(label="Cleaned: repo (branch)", errors=["dropdb failed", "branch -D failed"])
        assert result.clean is False
        assert str(result) == "Cleaned: repo (branch) [with errors: dropdb failed; branch -D failed]"

    def test_reexported_from_cleanup_module(self) -> None:
        assert ReExported is CleanupResult
