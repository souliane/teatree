from teatree.core.models.types import TicketExtra, WorktreeExtra, validated_ticket_extra, validated_worktree_extra


class TestValidatedTicketExtra:
    def test_none_returns_empty(self) -> None:
        assert validated_ticket_extra(None) == TicketExtra()

    def test_empty_dict_returns_empty(self) -> None:
        assert validated_ticket_extra({}) == TicketExtra()

    def test_recognized_keys_preserved(self) -> None:
        raw = {"tests_passed": True, "branch": "ac/fix-123", "labels": ["bug"]}
        result = validated_ticket_extra(raw)
        assert result["tests_passed"] is True
        assert result["branch"] == "ac/fix-123"
        assert result["labels"] == ["bug"]

    def test_unknown_keys_dropped(self) -> None:
        raw = {"tests_passed": True, "stale_key": "gone", "another": 42}
        result = validated_ticket_extra(raw)
        assert "stale_key" not in result
        assert "another" not in result
        assert result["tests_passed"] is True

    def test_prs_dict_preserved(self) -> None:
        raw = {"prs": {"123": {"url": "https://example.com", "title": "fix"}}}
        result = validated_ticket_extra(raw)
        assert "123" in result["prs"]


class TestValidatedWorktreeExtra:
    def test_none_returns_empty(self) -> None:
        assert validated_worktree_extra(None) == WorktreeExtra()

    def test_recognized_keys_preserved(self) -> None:
        raw = {"worktree_path": "/tmp/wt", "services": ["backend"]}
        result = validated_worktree_extra(raw)
        assert result["worktree_path"] == "/tmp/wt"

    def test_unknown_keys_dropped(self) -> None:
        raw = {"worktree_path": "/tmp/wt", "obsolete": True}
        result = validated_worktree_extra(raw)
        assert "obsolete" not in result
