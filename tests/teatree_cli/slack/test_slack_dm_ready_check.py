"""``check_and_render_dm_ready`` doctor renderer — surfacing-only, crash-proof."""

from unittest.mock import patch

import pytest

from teatree.cli.slack.dm_doctor import DmReadinessFinding, DmReadinessOutcome, check_and_render_dm_ready
from teatree.cli.slack.socket_doctor import Level


class TestRenderer:
    def test_prints_each_finding_with_level_and_overlay(self, capsys: pytest.CaptureFixture[str]) -> None:
        outcome = DmReadinessOutcome(
            findings=(
                DmReadinessFinding("t3", Level.WARN, "DM channel not provisioned yet"),
                DmReadinessFinding("t3", Level.OK, "Slack DM-ready"),
            )
        )
        with patch("teatree.cli.slack.dm_doctor.check_slack_dm_ready", return_value=outcome):
            assert check_and_render_dm_ready() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "DM channel not provisioned yet" in out
        assert "[t3]" in out

    def test_never_gates_exit_code_even_on_fail_findings(self, capsys: pytest.CaptureFixture[str]) -> None:
        outcome = DmReadinessOutcome(findings=(DmReadinessFinding("t3", Level.FAIL, "no slack_user_id"),))
        with patch("teatree.cli.slack.dm_doctor.check_slack_dm_ready", return_value=outcome):
            assert check_and_render_dm_ready() is True
        assert "FAIL" in capsys.readouterr().out

    def test_crash_degrades_to_warn(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("teatree.cli.slack.dm_doctor.check_slack_dm_ready", side_effect=RuntimeError("boom")):
            assert check_and_render_dm_ready() is True
        assert "WARN" in capsys.readouterr().out
