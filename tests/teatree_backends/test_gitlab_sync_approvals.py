"""Tests for detect_approval_dismissal in gitlab_sync_approvals."""

from teatree.backends.gitlab_sync_approvals import ApprovalDismissal, detect_approval_dismissal


def _disc(notes: list[dict]) -> dict:
    return {"notes": notes, "individual_note": len(notes) == 1}


def _system_note(body: str, *, username: str = "system", created_at: str) -> dict:
    return {
        "system": True,
        "body": body,
        "created_at": created_at,
        "author": {"username": username},
    }


def _user_note(body: str, *, username: str, created_at: str) -> dict:
    return {
        "system": False,
        "body": body,
        "created_at": created_at,
        "author": {"username": username},
    }


class TestDetectApprovalDismissal:
    def test_returns_none_when_currently_approved(self) -> None:
        """If current approval count > 0, no dismissal signal — re-approved already."""
        discussions = [
            _disc([_system_note("approved this merge request", username="alice", created_at="2026-05-01T10:00:00Z")]),
            _disc(
                [
                    _system_note(
                        "removed all approvals when new commits are added to the source branch",
                        created_at="2026-05-01T11:00:00Z",
                    ),
                ],
            ),
            _disc([_system_note("approved this merge request", username="bob", created_at="2026-05-01T12:00:00Z")]),
        ]
        assert detect_approval_dismissal(discussions, current_approval_count=1) is None

    def test_returns_none_when_no_dismissal_note(self) -> None:
        discussions = [
            _disc([_system_note("approved this merge request", username="alice", created_at="2026-05-01T10:00:00Z")]),
            _disc([_user_note("LGTM", username="alice", created_at="2026-05-01T10:30:00Z")]),
        ]
        assert detect_approval_dismissal(discussions, current_approval_count=0) is None

    def test_returns_none_when_no_prior_approval(self) -> None:
        """Spurious 'removed all approvals' without any prior approval is meaningless."""
        discussions = [
            _disc(
                [
                    _system_note(
                        "removed all approvals when new commits are added to the source branch",
                        created_at="2026-05-01T11:00:00Z",
                    ),
                ],
            ),
        ]
        assert detect_approval_dismissal(discussions, current_approval_count=0) is None

    def test_detects_push_reset_with_approver_attribution(self) -> None:
        discussions = [
            _disc([_system_note("approved this merge request", username="alice", created_at="2026-05-01T10:00:00Z")]),
            _disc([_system_note("approved this merge request", username="bob", created_at="2026-05-01T10:30:00Z")]),
            _disc(
                [
                    _system_note(
                        "removed all approvals when new commits are added to the source branch",
                        created_at="2026-05-01T11:00:00Z",
                    ),
                ],
            ),
        ]
        result = detect_approval_dismissal(discussions, current_approval_count=0)
        assert result == ApprovalDismissal(at="2026-05-01T11:00:00Z", approvers=["alice", "bob"])

    def test_handles_manual_unapproval_then_push_reset(self) -> None:
        """Manual unapproval before the push reset removes that user from the dismissed list."""
        discussions = [
            _disc([_system_note("approved this merge request", username="alice", created_at="2026-05-01T10:00:00Z")]),
            _disc([_system_note("approved this merge request", username="bob", created_at="2026-05-01T10:30:00Z")]),
            _disc(
                [_system_note("unapproved this merge request", username="alice", created_at="2026-05-01T10:45:00Z")],
            ),
            _disc(
                [
                    _system_note(
                        "removed all approvals because the merge request was updated",
                        created_at="2026-05-01T11:00:00Z",
                    ),
                ],
            ),
        ]
        result = detect_approval_dismissal(discussions, current_approval_count=0)
        assert result is not None
        assert result.approvers == ["bob"]

    def test_uses_most_recent_dismissal(self) -> None:
        """Two dismissal cycles: only the most recent matters."""
        discussions = [
            _disc([_system_note("approved this merge request", username="alice", created_at="2026-05-01T10:00:00Z")]),
            _disc(
                [
                    _system_note(
                        "removed all approvals when new commits are added to the source branch",
                        created_at="2026-05-01T11:00:00Z",
                    ),
                ],
            ),
            _disc([_system_note("approved this merge request", username="bob", created_at="2026-05-01T12:00:00Z")]),
            _disc(
                [
                    _system_note(
                        "removed all approvals when new commits are added to the source branch",
                        created_at="2026-05-01T13:00:00Z",
                    ),
                ],
            ),
        ]
        result = detect_approval_dismissal(discussions, current_approval_count=0)
        assert result == ApprovalDismissal(at="2026-05-01T13:00:00Z", approvers=["bob"])

    def test_ignores_user_notes(self) -> None:
        """Non-system notes with similar text must not trigger detection."""
        discussions = [
            _disc([_system_note("approved this merge request", username="alice", created_at="2026-05-01T10:00:00Z")]),
            _disc(
                [
                    _user_note(
                        "I removed all approvals because the bot was wrong",
                        username="bob",
                        created_at="2026-05-01T11:00:00Z",
                    ),
                ],
            ),
        ]
        assert detect_approval_dismissal(discussions, current_approval_count=0) is None
