"""``normalize`` builds the SAME ``IngestionRecord`` shape for every subscribed GitHub event family (#4795).

The one normalizer both the webhook view and the ``github_polling`` scanner call.
"""

from teatree.core.github_app.webhook_normalize import normalize


class TestPullRequestNormalize:
    def test_extracts_actor_channel_thread_and_body(self) -> None:
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "title": "Add feature", "updated_at": "2026-09-25T10:00:00Z"},
            "sender": {"login": "alice"},
        }
        record = normalize("pull_request", payload)
        assert record.actor == "alice"
        assert record.channel_ref == "owner/repo"
        assert record.thread_ref == "17"
        assert record.body == "Add feature"
        assert record.payload_json == payload

    def test_review_actor_falls_back_to_review_user(self) -> None:
        payload = {
            "action": "submitted",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "title": "Add feature"},
            "review": {"state": "approved", "user": {"login": "bob"}},
        }
        record = normalize("pull_request_review", payload)
        assert record.actor == "bob"


class TestIssueNormalize:
    def test_issue_uses_issue_number_and_title(self) -> None:
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 5, "title": "Bug report"},
            "sender": {"login": "carol"},
        }
        record = normalize("issues", payload)
        assert record.thread_ref == "5"
        assert record.body == "Bug report"
        assert record.actor == "carol"

    def test_issue_comment_body_is_the_comment_text(self) -> None:
        payload = {
            "action": "created",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 5, "title": "Bug report"},
            "comment": {"id": 1, "body": "I can reproduce this"},
            "sender": {"login": "carol"},
        }
        record = normalize("issue_comment", payload)
        assert record.thread_ref == "5"
        assert record.body == "I can reproduce this"


class TestPushNormalize:
    def test_push_uses_ref_as_thread_and_head_commit_message_as_body(self) -> None:
        payload = {
            "ref": "refs/heads/main",
            "after": "deadbeef",
            "repository": {"full_name": "owner/repo"},
            "pusher": {"name": "dave"},
            "head_commit": {"message": "fix: correct the thing"},
        }
        record = normalize("push", payload)
        assert record.thread_ref == "refs/heads/main"
        assert record.body == "fix: correct the thing"
        assert record.actor == "dave"


class TestCheckRunNormalize:
    def test_check_run_body_is_the_conclusion_or_status(self) -> None:
        payload = {
            "action": "completed",
            "repository": {"full_name": "owner/repo"},
            "check_run": {"id": 42, "name": "CI", "status": "completed", "conclusion": "success"},
            "sender": {"login": "eve"},
        }
        record = normalize("check_run", payload)
        assert record.thread_ref == "42"
        assert "success" in record.body


class TestIdempotencyKeyDerivation:
    def test_identity_derived_key_when_available(self) -> None:
        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 17, "title": "x", "updated_at": "2026-09-25T10:00:00Z"},
        }
        record = normalize("pull_request", payload, delivery_id="abc-123")
        assert record.idempotency_key == "github:pr:owner/repo:17:2026-09-25T10:00:00Z"

    def test_falls_back_to_delivery_id_when_no_identity_derivable(self) -> None:
        payload = {"action": "opened", "sender": {"login": "x"}, "repository": {"full_name": "owner/repo"}}
        record = normalize("pull_request", payload, delivery_id="abc-123")
        assert record.idempotency_key == "github:delivery:abc-123"

    def test_falls_back_to_a_payload_hash_when_no_delivery_id_either(self) -> None:
        payload = {"action": "opened"}
        record = normalize("ping", payload, delivery_id="")
        assert record.idempotency_key.startswith("github:delivery:")
        assert len(record.idempotency_key) > len("github:delivery:")
