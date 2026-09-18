import logging

import pytest

from teatree.core.models import types
from teatree.core.models.types import (
    SlackAnswerContext,
    TicketExtra,
    WorktreeExtra,
    validated_ticket_extra,
    validated_worktree_extra,
)


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

    def test_reopen_revivals_survives_validation(self) -> None:
        """Undeclared, the board's revival counter is stripped by every ladder transition (#4152)."""
        assert validated_ticket_extra({"reopen_revivals": 2})["reopen_revivals"] == 2


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


class TestStrippedKeysAreAnnounced:
    """A silent strip is how #4152 and #2663 stayed hidden for months — it must be loud."""

    @pytest.fixture(autouse=True)
    def _fresh_warning_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The real dedup set is process-wide by design, so each lane needs its own."""
        monkeypatch.setattr(types, "_WARNED_STRIPPED_KEYS", set())

    @staticmethod
    def _dropped(model: str, key: str) -> str:
        return (
            f"{model}.extra: dropping undeclared key '{key}' — "
            f"declare it on {model}Extra or the next transition strips it."
        )

    def test_slack_answer_survives_validation(self) -> None:
        """Undeclared, the reactive cycle's whole Slack context went with the first transition (#2663)."""
        context: SlackAnswerContext = {"question": "why?", "work_issue_url": "https://example.com/1"}
        assert validated_ticket_extra({"slack_answer": context})["slack_answer"] == context

    def test_directive_marker_survives_validation(self) -> None:
        """``Directive.for_ticket`` resolves this marker BEFORE the reverse FK (#2663)."""
        assert validated_ticket_extra({"directive_id": 7})["directive_id"] == 7

    def test_an_undeclared_key_is_logged_once_per_process(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=types.__name__):
            validated_ticket_extra({"zz_undeclared": 1})
            validated_ticket_extra({"zz_undeclared": 2})

        assert [record.getMessage() for record in caplog.records] == [self._dropped("Ticket", "zz_undeclared")]

    def test_each_undeclared_key_earns_its_own_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=types.__name__):
            validated_ticket_extra({"zz_one": 1, "zz_two": 2, "branch": "declared-so-silent"})

        assert sorted(record.getMessage() for record in caplog.records) == [
            self._dropped("Ticket", "zz_one"),
            self._dropped("Ticket", "zz_two"),
        ]

    def test_the_worktree_validator_names_its_own_model(self, caplog: pytest.LogCaptureFixture) -> None:
        """The dedup key is the (model, key) pair: one model's warning must not silence the other's."""
        with caplog.at_level(logging.WARNING, logger=types.__name__):
            validated_ticket_extra({"zz_shared": 1})
            validated_worktree_extra({"zz_shared": 1})

        assert [record.getMessage() for record in caplog.records] == [
            self._dropped("Ticket", "zz_shared"),
            self._dropped("Worktree", "zz_shared"),
        ]

    def test_a_fully_declared_extra_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=types.__name__):
            validated_ticket_extra({"branch": "main", "tests_passed": True})

        assert caplog.records == []
