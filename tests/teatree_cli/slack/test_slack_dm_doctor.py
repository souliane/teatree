"""``t3 doctor`` Slack DM CONFIG completeness — fail-loud per-overlay config diagnosis.

Scope check included: the OK line must read as "config filled in", never as
"notifications work". This check resolves each overlay BY NAME while the headless
egress resolves ambiently, so the two can disagree — deliverability is the Slack
round-trip gate's job, not this one's.

Overlays are sourced from the DB ``overlays`` registry (read via the Django ORM
by the doctor, which runs after ``ensure_django``), so the cases seed via
:func:`_seed`. The resolved messaging backend is patched at the module boundary
so no live Slack call is made.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.cli.slack.dm_doctor import check_slack_dm_ready
from teatree.cli.slack.socket_doctor import Level
from teatree.core.models import ConfigSetting


class _FakeSlackBackend:
    """Stand-in for a resolved ``SlackBotBackend`` (any non-noop backend)."""


def _seed(overlays: dict[str, dict]) -> None:
    ConfigSetting.objects.set_value("overlays", overlays)


class TestDmReadiness(TestCase):
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
        assert any(f.level is Level.OK and "Slack DM config complete" in f.message for f in outcome.findings)

    def test_the_ok_line_cannot_be_read_as_a_promise_that_notifications_deliver(self) -> None:
        """This check resolves BY NAME; the runtime resolves ambiently, so it proves config only.

        Wording it as readiness is what let a green doctor sit beside a totally dead
        notification path for a day — the operator read "DM-ready" as "DMs work".
        """
        _seed({"t3": {"messaging_backend": "slack", "slack_user_id": "U1", "slack_dm_channel_id": "D1"}})
        with patch("teatree.cli.slack.dm_doctor.messaging_from_overlay", return_value=_FakeSlackBackend()):
            outcome = check_slack_dm_ready()
        message = next(f.message for f in outcome.findings if f.level is Level.OK)
        assert "config only" in message
        assert "NOT proof a DM delivers" in message
        assert "DM-ready" not in message

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
