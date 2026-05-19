"""End-to-end test that ``notify_user`` records an OutboundClaim (#1019)."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.models import OutboundClaim
from teatree.notify import NotifyKind, notify_user


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
            backend=backend,
            user_id="U_ME",
        )
        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:failed-key").exists()

    def test_disabled_feature_does_not_record_claim(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        backend = _backend()
        fake_settings = MagicMock()
        fake_settings.notify_user_via_bot = False
        with patch("teatree.notify.get_effective_settings", return_value=fake_settings):
            notify_user(
                "shh",
                kind=NotifyKind.INFO,
                idempotency_key="disabled-key",
                backend=backend,
                user_id="U_ME",
            )
        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:disabled-key").exists()
