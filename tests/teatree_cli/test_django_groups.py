from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from typer.testing import CliRunner

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.cli.overlay import OverlayAppBuilder
from teatree.core.management.commands.github_webhook import Command as GithubWebhookCommand
from teatree.core.management.commands.secrets import Command as SecretsCommand


def test_github_webhook_subcommands_are_reachable_from_overlay(tmp_path: Path) -> None:
    registered = {
        (item.name or (item.callback.__name__ if item.callback else "")).replace("_", "-")
        for item in GithubWebhookCommand.typer_app.registered_commands
        if item.name or item.callback
    }
    group = DJANGO_GROUPS["github-webhook"]
    assert {name for name, _ in group.subcommands} == registered
    assert group.core_dispatch
    app = OverlayAppBuilder(overlay_name="example", project_path=tmp_path).build()
    with patch("teatree.cli.overlay.run_streamed") as run:
        result = CliRunner().invoke(
            app,
            ["github-webhook", "connect", "--repo", "sample/repo", "--url", "https://hooks.example.org/hooks/github/"],
        )
    assert result.exit_code == 0, result.output
    argv = run.call_args.args[0]
    assert argv[-6:] == [
        "github_webhook",
        "connect",
        "--repo",
        "sample/repo",
        "--url",
        "https://hooks.example.org/hooks/github/",
    ]
    assert "-m" in argv
    assert "teatree" in argv


class SecretsGroupWiringTest(SimpleTestCase):
    def test_exec_bridge_preserves_the_command_separator(self) -> None:
        with patch("teatree.cli.overlay.managepy_core") as bridge:
            result = CliRunner().invoke(
                OverlayAppBuilder("teatree", None).build(),
                ["secrets", "exec", "--env", "TOKEN=team/token", "--", "python", "-c", "print(1)"],
            )
        assert result.exit_code == 0, result.output
        bridge.assert_called_once_with(
            "secrets", "exec", "--env", "TOKEN=team/token", "--", "python", "-c", "print(1)", overlay_name="teatree"
        )

    def test_catalogue_matches_every_registered_command(self) -> None:
        catalogue = {name for name, _ in DJANGO_GROUPS["secrets"].subcommands}
        registered = {
            (item.name or item.callback.__name__).replace("_", "-")
            for item in SecretsCommand.typer_app.registered_commands
            if item.name or item.callback
        }
        assert catalogue == registered
        assert {"set", "rm", "verify", "get", "exec"} <= catalogue
