"""Predicate that enforces the 4 skip-conditions for review candidates (#1321).

The auto-sweep / discover surfaces previously relied on agent-side BINDING
memory to apply these rules; this module is the canonical structural fix.
"""

from teatree.core.review_candidate import should_review_candidate, should_review_candidate_reasons


class TestSkipSelfAuthor:
    def test_skip_when_gitlab_author_is_current_user(self) -> None:
        mr = {"author": {"username": "alice"}, "state": "opened", "notes": []}
        assert should_review_candidate(mr, current_user="alice") is False
        assert "author_is_self" in should_review_candidate_reasons(mr, current_user="alice")

    def test_skip_when_github_user_is_current_user(self) -> None:
        mr = {"user": {"login": "alice"}, "state": "open"}
        assert should_review_candidate(mr, current_user="alice") is False

    def test_keeps_mr_authored_by_another_user(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": []}
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipAlreadyApproved:
    def test_skip_when_mr_approved_flag_set_and_user_in_approvers(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approved": True,
            "approvers": [{"username": "alice"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False
        assert "already_approved_by_self" in should_review_candidate_reasons(mr, current_user="alice")

    def test_skip_when_current_user_in_approvers_even_without_approved_flag(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": [{"username": "alice"}, {"username": "carol"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_keeps_mr_approved_only_by_others(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approved": True,
            "approvers": [{"username": "carol"}],
        }
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipMyNoteExists:
    def test_skip_when_current_user_has_non_system_note(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [
                {"author": {"username": "alice"}, "system": False, "body": "looks good"},
            ],
        }
        assert should_review_candidate(mr, current_user="alice") is False
        assert "has_self_note" in should_review_candidate_reasons(mr, current_user="alice")

    def test_keeps_mr_when_only_system_notes_from_user(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [
                {"author": {"username": "alice"}, "system": True, "body": "assigned"},
            ],
        }
        assert should_review_candidate(mr, current_user="alice") is True

    def test_keeps_mr_when_only_other_users_commented(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [
                {"author": {"username": "carol"}, "system": False, "body": "nit"},
            ],
        }
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipMergedOrClosed:
    def test_skip_merged(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "merged"}
        assert should_review_candidate(mr, current_user="alice") is False
        assert "state_merged" in should_review_candidate_reasons(mr, current_user="alice")

    def test_skip_closed(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "closed"}
        assert should_review_candidate(mr, current_user="alice") is False
        assert "state_closed" in should_review_candidate_reasons(mr, current_user="alice")

    def test_keeps_open_mr(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipBroadcastReactedByOthers:
    def test_skip_when_broadcast_has_eyes_from_other_user(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": ["U_BOB"]}]}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is False
        assert "broadcast_reacted_by_other" in should_review_candidate_reasons(
            mr, current_user="U_ALICE", broadcast=broadcast
        )

    def test_skip_when_broadcast_has_white_check_mark_from_other(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "white_check_mark", "users": ["U_OTHER"]}]}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is False

    def test_keeps_mr_when_broadcast_reaction_only_from_self(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": ["U_ALICE"]}]}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is True

    def test_keeps_mr_when_broadcast_has_no_reactions(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": []}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is True

    def test_keeps_mr_when_broadcast_none(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=None) is True


class TestShapeTolerance:
    """Heterogeneous and malformed inputs should not crash the predicate."""

    def test_mr_without_author_or_user_keys(self) -> None:
        mr = {"state": "opened"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_bare_string_approvers(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": ["alice", "carol"],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_mr_with_login_keyed_approver(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": [{"login": "alice"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_mr_with_name_keyed_approver(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": [{"name": "alice"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_mr_with_non_sequence_approvers(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "approvers": "alice"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_non_dict_note_element(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": ["bad-shape"]}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_non_sequence_notes(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": "not-a-list"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_unknown_state_string_is_open(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "draft"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_non_string_state(self) -> None:
        mr = {"author": {"username": "bob"}, "state": 42}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_broadcast_with_non_sequence_reactions(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": "not-a-list"}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is True

    def test_broadcast_with_non_dict_reaction_entry(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": ["malformed"]}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is True

    def test_broadcast_with_non_sequence_users(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": "U_OTHER"}]}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is True

    def test_broadcast_user_is_empty_string(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": ["", "U_OTHER"]}]}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is False

    def test_note_with_non_dict_author(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [{"author": "alice-as-string", "system": False, "body": "x"}],
        }
        assert should_review_candidate(mr, current_user="alice") is True


class TestReasonsAccumulate:
    def test_multiple_skip_reasons_returned(self) -> None:
        mr = {
            "author": {"username": "alice"},
            "state": "merged",
            "approved": True,
            "approvers": [{"username": "alice"}],
            "notes": [{"author": {"username": "alice"}, "system": False, "body": "x"}],
        }
        reasons = should_review_candidate_reasons(mr, current_user="alice")
        assert "author_is_self" in reasons
        assert "already_approved_by_self" in reasons
        assert "has_self_note" in reasons
        assert "state_merged" in reasons

    def test_clean_candidate_returns_empty_reasons(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": []}
        assert should_review_candidate_reasons(mr, current_user="alice") == []
