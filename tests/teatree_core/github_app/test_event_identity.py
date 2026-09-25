"""``identity_for`` collapses a webhook delivery and a poll discovery onto one identity (#4795).

A shared entity, observed via either transport, must resolve to the SAME
identity so the two transports never double-ingest the same logical update.
"""

from teatree.core.github_app.event_identity import entity_key_and_marker, identity_for


class TestPullRequestIdentity:
    def _payload(self, *, number: int = 17, updated_at: str = "2026-09-25T10:00:00Z") -> dict:
        return {
            "action": "synchronize",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": number, "updated_at": updated_at, "title": "Add feature"},
        }

    def test_webhook_and_poll_shaped_payloads_collapse_to_the_same_identity(self) -> None:
        webhook_shape = self._payload()
        # A poll-discovered PR has the same entity fields but none of the webhook
        # envelope (no ``action``, no ``sender``) — identity must not depend on those.
        poll_shape = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "updated_at": "2026-09-25T10:00:00Z", "title": "Add feature"},
        }
        assert identity_for("pull_request", webhook_shape) == identity_for("pull_request", poll_shape)

    def test_a_later_update_changes_the_identity(self) -> None:
        first = identity_for("pull_request", self._payload(updated_at="2026-09-25T10:00:00Z"))
        second = identity_for("pull_request", self._payload(updated_at="2026-09-25T11:00:00Z"))
        assert first != second

    def test_a_different_pr_number_changes_the_identity(self) -> None:
        first = identity_for("pull_request", self._payload(number=1))
        second = identity_for("pull_request", self._payload(number=2))
        assert first != second

    def test_pull_request_review_reads_the_same_pull_request_object(self) -> None:
        payload = self._payload()
        payload["action"] = "submitted"
        payload["review"] = {"state": "approved", "user": {"login": "bob"}}
        assert identity_for("pull_request_review", payload) == identity_for("pull_request", self._payload())

    def test_missing_updated_at_yields_no_identity(self) -> None:
        payload = {"repository": {"full_name": "owner/repo"}, "pull_request": {"number": 17}}
        assert identity_for("pull_request", payload) is None


class TestIssueIdentity:
    def test_issue_identity_keys_on_number_and_updated_at(self) -> None:
        payload = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 5, "updated_at": "2026-09-25T09:00:00Z"},
        }
        assert identity_for("issues", payload) == identity_for("issues", dict(payload))

    def test_issue_comment_keys_on_comment_id_not_issue_number(self) -> None:
        base = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 5},
            "comment": {"id": 999, "updated_at": "2026-09-25T09:00:00Z"},
        }
        other_comment = {
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 5},
            "comment": {"id": 1000, "updated_at": "2026-09-25T09:00:00Z"},
        }
        assert identity_for("issue_comment", base) != identity_for("issue_comment", other_comment)


class TestPushIdentity:
    def test_push_identity_keys_on_ref_and_after_sha(self) -> None:
        payload = {"repository": {"full_name": "owner/repo"}, "ref": "refs/heads/main", "after": "deadbeef"}
        assert identity_for("push", payload) == identity_for("push", dict(payload))

    def test_a_different_after_sha_changes_the_identity(self) -> None:
        first = {"repository": {"full_name": "owner/repo"}, "ref": "refs/heads/main", "after": "aaa"}
        second = {"repository": {"full_name": "owner/repo"}, "ref": "refs/heads/main", "after": "bbb"}
        assert identity_for("push", first) != identity_for("push", second)


class TestCheckRunIdentity:
    def test_check_run_identity_keys_on_id_status_and_completed_at(self) -> None:
        payload = {
            "repository": {"full_name": "owner/repo"},
            "check_run": {"id": 42, "status": "completed", "completed_at": "2026-09-25T09:05:00Z"},
        }
        assert identity_for("check_run", payload) == identity_for("check_run", dict(payload))

    def test_in_progress_uses_started_at_when_completed_at_is_absent(self) -> None:
        payload = {
            "repository": {"full_name": "owner/repo"},
            "check_run": {"id": 42, "status": "in_progress", "started_at": "2026-09-25T09:00:00Z"},
        }
        assert identity_for("check_run", payload) is not None


class TestUnrecognisedEvent:
    def test_unmapped_event_type_yields_no_identity(self) -> None:
        assert identity_for("installation", {"repository": {"full_name": "owner/repo"}}) is None

    def test_missing_repository_yields_no_identity(self) -> None:
        assert identity_for("pull_request", {"pull_request": {"number": 1, "updated_at": "x"}}) is None


class TestEntityKeyAndMarker:
    def test_same_entity_two_markers_share_the_entity_key(self) -> None:
        payload = {"repository": {"full_name": "owner/repo"}, "pull_request": {"number": 17, "updated_at": "t1"}}
        earlier = entity_key_and_marker("pull_request", payload)
        payload["pull_request"]["updated_at"] = "t2"
        later = entity_key_and_marker("pull_request", payload)
        assert earlier is not None
        assert later is not None
        assert earlier[0] == later[0]
        assert earlier[1] != later[1]

    def test_composed_identity_is_entity_key_colon_marker(self) -> None:
        payload = {"repository": {"full_name": "owner/repo"}, "pull_request": {"number": 17, "updated_at": "t1"}}
        found = entity_key_and_marker("pull_request", payload)
        assert found is not None
        key, marker = found
        assert identity_for("pull_request", payload) == f"{key}:{marker}"

    def test_a_timestamp_marker_containing_colons_stays_intact(self) -> None:
        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "updated_at": "2026-09-25T10:30:15Z"},
        }
        found = entity_key_and_marker("pull_request", payload)
        assert found is not None
        key, marker = found
        assert marker == "2026-09-25T10:30:15Z"
        assert key == "github:pr:owner/repo:17"
