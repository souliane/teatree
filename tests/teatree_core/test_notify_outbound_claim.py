"""End-to-end test that ``notify_user`` records an OutboundClaim (#1019)."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.models import OutboundClaim
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.modelkit.notify_policy import NotifyAudience


def _backend(*, permalink: str = "https://acme.slack.com/archives/D-USER/p1700000000000000") -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = permalink
    return b


class NotifyUserRecordsOutboundClaimTests(TestCase):
    def test_successful_dm_records_slack_dm_claim_with_permalink(self) -> None:
        backend = _backend()
        sent = notify_user(
            "tests passing on s-1019",
            kind=NotifyKind.INFO,
            idempotency_key="sess=a;turn=1",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        claim = OutboundClaim.objects.get(idempotency_key="slack_dm:sess=a;turn=1")
        assert claim.kind == OutboundClaim.Kind.SLACK_DM
        assert claim.target_url.startswith("https://")
        assert claim.extra.get("channel") == "D-USER"
        assert claim.extra.get("ts") == "1700000000.000000"
        assert claim.verified_at is None
        assert claim.drift_detected is False

    def test_failed_dm_does_not_record_claim(self) -> None:
        backend = _backend()
        backend.post_message.side_effect = RuntimeError("transport boom")
        notify_user(
            "won't post",
            kind=NotifyKind.INFO,
            idempotency_key="failed-key",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=backend,
            user_id="U_ME",
        )
        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:failed-key").exists()

    def test_disabled_feature_does_not_record_claim(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        backend = _backend()
        fake_settings = MagicMock()
        fake_settings.notify_user_via_bot = False
        # `notify_user` lives in `teatree.core.notify` since #1009 — the
        # top-level `teatree.core.notify` is a thin re-export. Patch where the
        # real `get_effective_settings` is bound.
        with patch("teatree.core.notify.get_effective_settings", return_value=fake_settings):
            notify_user(
                "shh",
                kind=NotifyKind.INFO,
                idempotency_key="disabled-key",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:disabled-key").exists()

    def test_slack_dm_claim_records_agent_session_id(self) -> None:
        # Mirrors the GitLab/Notion path's
        # ``test_resolves_agent_session_id_from_env`` (#1065 Nit 1): the
        # inline Slack-DM writer must populate ``agent_session_id`` so the
        # audit ledger's cross-reference stays symmetric with the rows
        # ``record_claim`` writes.
        from unittest.mock import patch  # noqa: PLC0415

        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "sess-123"}, clear=False):
            sent = notify_user(
                "tests passing",
                kind=NotifyKind.INFO,
                idempotency_key="sess=z;turn=1",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=_backend(),
                user_id="U_ME",
            )
        assert sent is True
        claim = OutboundClaim.objects.get(idempotency_key="slack_dm:sess=z;turn=1")
        assert claim.agent_session_id == "sess-123"

    def test_database_error_is_swallowed_publish_path_unbroken(self) -> None:
        # #1065 Nit 2.2: a DB outage of the audit ledger must degrade the
        # claim record to a no-op (warning log) without turning the
        # already-succeeded Slack post into a user-visible failure.
        from unittest.mock import patch  # noqa: PLC0415

        from django.db import DatabaseError  # noqa: PLC0415

        with patch.object(
            OutboundClaim.objects,
            "get_or_create",
            side_effect=DatabaseError("ledger unavailable"),
        ):
            sent = notify_user(
                "tests passing",
                kind=NotifyKind.INFO,
                idempotency_key="db-error-key",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=_backend(),
                user_id="U_ME",
            )
        assert sent is True
        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:db-error-key").exists()
