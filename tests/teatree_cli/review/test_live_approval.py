from unittest.mock import MagicMock, patch

from click.testing import Result
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.models import LivePostApproval, OnBehalfApproval, OperatorIdentity, Ticket


class TestRequesterBoundLiveApproval(TestCase):
    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password=None)
        OperatorIdentity.objects.create(user=user, platform="gitlab", handle="alice", external_id="101")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-ALICE")
        Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/merge_requests/7",
            requested_by="gitlab:alice",
        )
        self.backend = MagicMock()
        self.backend.open_dm.return_value = "D-ALICE"

    def _invoke(self, *args: str) -> Result:
        with (
            patch("teatree.core.notify.resolve_user_id", return_value="U-OWNER"),
            patch("teatree.core.notify.resolve_user_channel", return_value="D-OWNER"),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=self.backend),
        ):
            return CliRunner().invoke(app, ["review", "approve-live-post", "org/repo!7", *args])

    def test_slack_approval_uses_requesters_dm_and_author(self) -> None:
        ts = f"{timezone.now().timestamp():.4f}"
        self.backend.fetch_message.return_value = {"user": "U-ALICE", "text": "go ahead"}

        result = self._invoke("--slack-ts", ts)

        assert result.exit_code == 0, result.output
        self.backend.open_dm.assert_called_once_with("U-ALICE")
        assert LivePostApproval.objects.get(mr_url="org/repo!7").slack_user_id == "U-ALICE"

    def test_recorded_approval_uses_requesters_identity(self) -> None:
        OnBehalfApproval.record("org/repo!7", "post_comment", "gitlab:alice")

        result = self._invoke("--from-on-behalf")

        assert result.exit_code == 0, result.output
        assert LivePostApproval.objects.get(mr_url="org/repo!7").slack_user_id == "U-ALICE"

    def test_other_persons_recorded_approval_cannot_authorize_requester(self) -> None:
        OnBehalfApproval.record("org/repo!7", "post_comment", "gitlab:bob")

        result = self._invoke("--from-on-behalf")

        assert result.exit_code == 1
        assert not LivePostApproval.objects.exists()
