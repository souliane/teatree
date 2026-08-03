"""Tests for the top-level ``t3 push`` CLI command (souliane/teatree#3927)."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.push import push
from teatree.core.forge_push import PUSH_EXIT_CODES, CredentialSource, PushFailure, PushOutcome
from tests._git_repo import make_git_repo, run_git

runner = CliRunner()

_app = typer.Typer()
_app.command()(push)


def _exits_zero_without_pushing(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


def _success() -> PushOutcome:
    return PushOutcome(
        ok=True,
        branch="feature",
        remote="origin",
        credential_source=CredentialSource.GH_TOKEN,
        pushed_sha="a" * 40,
        detail="",
    )


def _refusal(failure: PushFailure = PushFailure.CONFIG) -> PushOutcome:
    return PushOutcome(
        ok=False,
        branch="feature",
        remote="origin",
        credential_source=CredentialSource.AMBIENT,
        detail="remote 'origin' embeds a credential in its URL",
        failure=failure,
    )


class TestPushCommand:
    def test_success_reports_branch_sha_and_credential_source(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_success()):
            result = runner.invoke(_app, [])
        assert result.exit_code == 0
        assert "feature" in result.output
        assert CredentialSource.GH_TOKEN.value in result.output

    def test_refusal_prints_the_reason_and_exits_with_the_failures_own_code(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_refusal()):
            result = runner.invoke(_app, [])
        assert result.exit_code == PUSH_EXIT_CODES[PushFailure.CONFIG]
        assert "REFUSED" in result.output
        assert "embeds a credential" in result.output

    def test_a_gate_refusal_and_a_credential_failure_exit_differently(self) -> None:
        """Two different fixes must not arrive as one rc=1 (#4076)."""
        codes = {}
        for failure in (PushFailure.GATE_REFUSED, PushFailure.CREDENTIAL, PushFailure.NON_FAST_FORWARD):
            with patch("teatree.cli.push.push_branch", return_value=_refusal(failure)):
                result = runner.invoke(_app, [])
            codes[failure] = result.exit_code
            assert failure.value in result.output

        assert len(set(codes.values())) == len(codes)
        assert 0 not in codes.values()

    def test_an_unclassified_refusal_still_exits_non_zero(self) -> None:
        """Fail closed: a producer that forgot the kind must not read as delivered."""
        with patch("teatree.cli.push.push_branch", return_value=_refusal(PushFailure.NONE)):
            result = runner.invoke(_app, [])
        assert result.exit_code != 0

    def test_json_carries_the_failure_kind_and_the_exit_code(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_refusal(PushFailure.GATE_REFUSED)):
            result = runner.invoke(_app, ["--json"])
        report = json.loads(result.output)
        assert report["failure"] == PushFailure.GATE_REFUSED.value
        assert report["exit_code"] == result.exit_code != 0

    def test_json_emits_the_outcome_dict(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_success()):
            result = runner.invoke(_app, ["--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["credential_source"] == CredentialSource.GH_TOKEN.value

    def test_a_push_that_never_landed_exits_non_zero(self, tmp_path: Path) -> None:
        """rc=0 from `git push` is not delivery — the exit code must not say it was (#4088)."""
        remote = make_git_repo(tmp_path / "origin.git", bare=True)
        clone = make_git_repo(tmp_path / "clone", default_branch="feature")
        run_git(clone, "remote", "add", "origin", str(remote))

        with patch("teatree.core.forge_push.run_allowed_to_fail", _exits_zero_without_pushing):
            result = runner.invoke(_app, ["--repo", str(clone)])

        assert not run_git(clone, "ls-remote", "--heads", "origin")
        assert result.exit_code != 0, result.output

    def test_forwards_every_option_to_the_engine(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_success()) as pushed:
            runner.invoke(_app, ["--repo", "/tmp/x", "--remote", "upstream", "--branch", "b", "--force-with-lease"])
        assert pushed.call_args.kwargs == {
            "repo": "/tmp/x",
            "remote": "upstream",
            "branch": "b",
            "force_with_lease": True,
        }
