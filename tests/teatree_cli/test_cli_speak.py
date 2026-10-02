"""``t3 speak`` refuses: local audio is a sink that cannot reach the user."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.speak import speak

runner = CliRunner()

_app = typer.Typer()
_app.command()(speak)


class TestSpeakRefuses:
    """Teatree runs headless, so a local-audio read never reaches an away user.

    Shelling out to it would lose the message, so the command is a no-op-with-warning
    and can't be a silent lost-contact vector — the sanctioned path is the Slack
    DeferredQuestion egress.
    """

    def test_it_is_a_no_op_with_a_warning(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call_mock:
            result = runner.invoke(_app, ["hello away user"])
        assert result.exit_code == 0
        call_mock.assert_not_called()
        assert "cannot reach the user" in result.output

    def test_the_overlay_flag_is_still_accepted(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call_mock:
            result = runner.invoke(_app, ["hi", "--overlay", "teatree"])
        assert result.exit_code == 0
        call_mock.assert_not_called()
