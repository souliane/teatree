"""Tests for the ``t3 dogfood`` Typer entry point (#1308).

The CLI group is a thin forwarder to the core management command —
these tests confirm the forwarding path is hit so latent import or
wiring breakage surfaces in the suite.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.dogfood import dogfood_app


def test_dogfood_overlay_provision_smoke_forwards_extra_args() -> None:
    """``t3 dogfood overlay-provision-smoke --foo bar`` reaches ``managepy_core``."""
    runner = CliRunner()
    with patch("teatree.cli.dogfood.managepy_core") as mock_forward:
        result = runner.invoke(
            dogfood_app,
            ["overlay-provision-smoke", "--overlay", "teatree", "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    mock_forward.assert_called_once()
    call_args = mock_forward.call_args.args
    assert call_args[0] == "dogfood"
    assert call_args[1] == "overlay-provision-smoke"
    # Extra args (everything after the sub-command name) are forwarded
    # verbatim to the management command.
    assert "--overlay" in call_args
    assert "teatree" in call_args
    assert "--dry-run" in call_args


def test_dogfood_top_level_no_args_exits_cleanly_with_help_hint() -> None:
    """``t3 dogfood`` (no args) prints help and exits 0 — never runs a sub-command."""
    runner = CliRunner()
    result = runner.invoke(dogfood_app, [])
    # The ``invoke_without_command`` callback keeps the app a real group (a
    # single-command Typer app would collapse into that command and run
    # ``overlay-provision-smoke`` on a bare invocation). A no-args call lands
    # in the callback, prints help, and exits 0 for the cron/loop recipe.
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_dogfood_overlay_provision_smoke_help_renders_without_crashing() -> None:
    """``t3 dogfood overlay-provision-smoke --help`` does not crash."""
    runner = CliRunner()
    with patch("teatree.cli.dogfood.managepy_core") as mock_forward:
        result = runner.invoke(dogfood_app, ["overlay-provision-smoke", "--help"])

    # The forwarder swallows ``--help`` and passes it through to the
    # management command (which prints its own help). Either path keeps
    # exit_code at 0 and does not raise.
    assert result.exit_code == 0
    # ``--help`` is forwarded just like any other extra arg.
    if mock_forward.called:
        assert "--help" in mock_forward.call_args.args
