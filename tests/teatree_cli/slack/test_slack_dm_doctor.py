"""``t3 doctor`` Slack DM-readiness — fail-loud per-overlay DM/read diagnosis.

Overlays are sourced from the DB ``overlays`` registry (read via the Django ORM
by the doctor, which runs after ``ensure_django``), so the cases seed via
:func:`_seed`. The resolved messaging backend is patched at the module boundary
so no live Slack call is made.
"""

from unittest.mock import patch

import pytest

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.cli.slack.dm_doctor import check_slack_dm_ready
from teatree.cli.slack.socket_doctor import Level
from teatree.core.models import ConfigSetting


class _FakeSlackBackend:
    """Stand-in for a resolved ``SlackBotBackend`` (any non-noop backend)."""


def _seed(overlays: dict[str, dict]) -> None:
    ConfigSetting.objects.set_value("overlays", overlays)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDmReadiness:
    def test_noop_backend_despite_slack_is_fail(self) -> None:
        _seed({"t3": {"messaging_backend": "slack", "slack_user_id": "U1", "slack_dm_channel_id": "D1"}})
        with patch("teatree.cli.slack.dm_doctor.messaging_from_overlay", return_value=NoopMessagingBackend()):
            outcome = check_slack_dm_ready()
        assert any(
            f.level is Level.FAIL and "no-op" in f.message and "slack_token_ref" in f.message for f in outcome.findings
        )
        assert outcome.ok is False

    def test_no_backend_resolved_is_fail(self) -> None:
        _seed({"t3": {"messaging_backend": "slack", "slack_user_id": "U1", "slack_dm_channel_id": "D1"}})
        with patch("teatree.cli.slack.dm_doctor.messaging_from_overlay", return_value=None):
            outcome = check_slack_dm_ready()
        assert any(f.level is Level.FAIL and "no-op" in f.message for f in outcome.findings)
        assert outcome.ok is False

    def test_empty_user_id_is_fail(self) -> None:
        _seed({"t3": {"messaging_backend": "slack", "slack_user_id": "", "slack_dm_channel_id": "D1"}})
        with patch("teatree.cli.slack.dm_doctor.messaging_from_overlay", return_value=_FakeSlackBackend()):
            outcome = check_slack_dm_ready()
        assert any(f.level is Level.FAIL and "slack_user_id" in f.message for f in outcome.findings)
        assert outcome.ok is False

    def test_empty_dm_channel_is_warn(self) -> None:
        _seed({"t3": {"messaging_backend": "slack", "slack_user_id": "U1"}})
        with patch("teatree.cli.slack.dm_doctor.messaging_from_overlay", return_value=_FakeSlackBackend()):
            outcome = check_slack_dm_ready()
        assert any(f.level is Level.WARN and "DM channel not provisioned" in f.message for f in outcome.findings)
        # A missing DM channel is not a hard failure — setup provisions it.
        assert outcome.ok

    def test_all_set_is_ok(self) -> None:
        _seed({"t3": {"messaging_backend": "slack", "slack_user_id": "U1", "slack_dm_channel_id": "D1"}})
        with patch("teatree.cli.slack.dm_doctor.messaging_from_overlay", return_value=_FakeSlackBackend()):
            outcome = check_slack_dm_ready()
        assert outcome.ok
        assert any(f.level is Level.OK and "DM-ready" in f.message for f in outcome.findings)

    def test_no_slack_overlays_yields_no_findings(self) -> None:
        _seed({"t3": {"path": "/repo"}})
        assert check_slack_dm_ready().findings == ()

    def test_one_overlay_crash_does_not_abort_others(self) -> None:
        _seed(
            {
                "t3": {"messaging_backend": "slack", "slack_user_id": "U1", "slack_dm_channel_id": "D1"},
                "two": {"messaging_backend": "slack", "slack_user_id": "U2", "slack_dm_channel_id": "D2"},
            }
        )
        with patch(
            "teatree.cli.slack.dm_doctor.messaging_from_overlay",
            side_effect=[RuntimeError("boom"), _FakeSlackBackend()],
        ):
            outcome = check_slack_dm_ready()
        overlays_seen = {f.overlay for f in outcome.findings}
        assert overlays_seen == {"t3", "two"}
        assert any(f.overlay == "t3" and f.level is Level.WARN and "crashed" in f.message for f in outcome.findings)
