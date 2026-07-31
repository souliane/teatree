"""Tests for the top-level ``t3 push`` CLI command (souliane/teatree#3927)."""

import json
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.push import push
from teatree.core.forge_push import CredentialSource, PushOutcome

runner = CliRunner()

_app = typer.Typer()
_app.command()(push)


def _success() -> PushOutcome:
    return PushOutcome(
        ok=True,
        branch="feature",
        remote="origin",
        credential_source=CredentialSource.GH_TOKEN,
        pushed_sha="a" * 40,
        detail="",
    )


def _refusal() -> PushOutcome:
    return PushOutcome(
        ok=False,
        branch="feature",
        remote="origin",
        credential_source=CredentialSource.AMBIENT,
        detail="remote 'origin' embeds a credential in its URL",
    )


class TestPushCommand:
    def test_success_reports_branch_sha_and_credential_source(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_success()):
            result = runner.invoke(_app, [])
        assert result.exit_code == 0
        assert "feature" in result.output
        assert CredentialSource.GH_TOKEN.value in result.output

    def test_refusal_prints_the_reason_and_exits_1(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_refusal()):
            result = runner.invoke(_app, [])
        assert result.exit_code == 1
        assert "REFUSED" in result.output
        assert "embeds a credential" in result.output

    def test_json_emits_the_outcome_dict(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_success()):
            result = runner.invoke(_app, ["--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["credential_source"] == CredentialSource.GH_TOKEN.value

    def test_forwards_every_option_to_the_engine(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_success()) as pushed:
            runner.invoke(_app, ["--repo", "/tmp/x", "--remote", "upstream", "--branch", "b", "--force-with-lease"])
        assert pushed.call_args.kwargs == {
            "repo": "/tmp/x",
            "remote": "upstream",
            "branch": "b",
            "force_with_lease": True,
        }
