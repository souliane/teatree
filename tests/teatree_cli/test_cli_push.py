"""Tests for the top-level ``t3 push`` CLI command (souliane/teatree#3927)."""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.push import push
from teatree.core.forge_pr_probe import GithubOpenPrHead, PrProbe
from teatree.core.forge_push import PushOutcome
from teatree.core.forge_push_verdict import PUSH_EXIT_CODES, CredentialSource, PushFailure
from teatree.core.invocation_cwd import INVOCATION_CWD_ENV
from teatree.core.push.pr_head_check import PrHeadStatus, PrHeadVerdict, confirm_pr_head
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


@pytest.fixture(autouse=True)
def _open_pr_at_the_pushed_sha() -> Iterator[None]:
    verdict = PrHeadVerdict(PrHeadStatus.MATCHES, "the open PR is at the pushed sha")
    with patch("teatree.cli.push.confirm_pr_head", return_value=verdict):
        yield


class TestPushConfirmsThePullRequestHead:
    def test_an_open_pr_lagging_the_pushed_sha_exits_non_zero_naming_both_shas(self) -> None:
        lagging = "b" * 40
        github_head = GithubOpenPrHead(
            "https://github.com/souliane/teatree/pull/9", "souliane/teatree", "feature", lagging
        )

        with (
            patch("teatree.cli.push.push_branch", return_value=_success()),
            patch("teatree.cli.push.confirm_pr_head", wraps=confirm_pr_head),
            patch("teatree.core.push.pr_head_check.github_open_pr_head", return_value=github_head),
        ):
            result = runner.invoke(_app, [])

        assert result.exit_code == PUSH_EXIT_CODES[PushFailure.REMOTE_SHA_MISMATCH]
        assert "a" * 40 in result.output
        assert lagging in result.output

    def test_an_unreadable_pr_head_exits_non_zero_as_unverifiable(self) -> None:
        with (
            patch("teatree.cli.push.push_branch", return_value=_success()),
            patch("teatree.cli.push.confirm_pr_head", wraps=confirm_pr_head),
            patch("teatree.core.push.pr_head_check.github_open_pr_head", return_value=None),
            patch("teatree.core.push.pr_head_check.find_open_pr_for_branch", return_value=PrProbe.unknown()),
        ):
            result = runner.invoke(_app, [])

        assert result.exit_code == PUSH_EXIT_CODES[PushFailure.UNVERIFIABLE]
        assert PrHeadStatus.UNVERIFIABLE.value in result.output.lower()

    def test_no_open_pr_passes_and_says_so(self) -> None:
        with (
            patch("teatree.cli.push.push_branch", return_value=_success()),
            patch("teatree.cli.push.confirm_pr_head", wraps=confirm_pr_head),
            patch("teatree.core.push.pr_head_check.github_open_pr_head", return_value=None),
            patch("teatree.core.push.pr_head_check.find_open_pr_for_branch", return_value=PrProbe.none()),
        ):
            result = runner.invoke(_app, [])

        assert result.exit_code == 0
        assert "no open PR" in result.output

    def test_a_refused_push_never_reads_the_pr(self) -> None:
        with (
            patch("teatree.cli.push.push_branch", return_value=_refusal()),
            patch("teatree.cli.push.confirm_pr_head") as confirm,
        ):
            runner.invoke(_app, [])

        confirm.assert_not_called()

    def test_json_carries_the_pr_head_verdict(self) -> None:
        lagging = GithubOpenPrHead(
            "https://github.com/souliane/teatree/pull/9", "souliane/teatree", "feature", "b" * 40
        )

        with (
            patch("teatree.cli.push.push_branch", return_value=_success()),
            patch("teatree.cli.push.confirm_pr_head", wraps=confirm_pr_head),
            patch("teatree.core.push.pr_head_check.github_open_pr_head", return_value=lagging),
        ):
            result = runner.invoke(_app, ["--json"])

        assert json.loads(result.output)["pr_head"]["status"] == PrHeadStatus.LAGS.value


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

        with patch("teatree.core.forge_push.run_bounded_group", _exits_zero_without_pushing):
            result = runner.invoke(_app, ["--repo", str(clone)])

        assert not run_git(clone, "ls-remote", "--heads", "origin")
        assert result.exit_code != 0, result.output

    def test_the_help_names_every_failure_kind_and_its_exit_code(self) -> None:
        """The help is what an operator reads WHEN they get the code — docs-drift cannot catch it."""
        help_text = push.__doc__ or ""

        for failure, code in PUSH_EXIT_CODES.items():
            if failure is PushFailure.NONE:
                continue
            assert failure.value in help_text, failure
            assert str(code) in help_text, failure

    def test_forwards_every_option_to_the_engine(self) -> None:
        with patch("teatree.cli.push.push_branch", return_value=_success()) as pushed:
            runner.invoke(_app, ["--repo", "/tmp/x", "--remote", "upstream", "--branch", "b", "--force-with-lease"])
        assert pushed.call_args.kwargs == {
            "repo": "/tmp/x",
            "remote": "upstream",
            "branch": "b",
            "force_with_lease": True,
        }


def _clone_on_feature(root: Path) -> Path:
    remote = make_git_repo(root / "origin.git", bare=True)
    clone = make_git_repo(root / "clone", default_branch="feature")
    run_git(clone, "remote", "add", "origin", str(remote))
    return clone


def _remote_feature(clone: Path) -> str:
    return run_git(clone, "ls-remote", "origin", "refs/heads/feature").split("\t")[0]


class TestPushActsWhereTheOperatorStood:
    """Under ``deploy/t3`` the process cwd is the image WORKDIR; the operator's checkout arrives declared."""

    def test_a_push_from_the_container_workdir_reaches_the_declared_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _clone_on_feature(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(clone))

        result = runner.invoke(_app, [])

        assert result.exit_code == 0, result.output
        assert _remote_feature(clone) == run_git(clone, "rev-parse", "HEAD")

    def test_a_checkout_the_process_stands_in_beats_an_inherited_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stood_in = _clone_on_feature(tmp_path / "a")
        inherited = _clone_on_feature(tmp_path / "b")
        monkeypatch.chdir(stood_in)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(inherited))

        result = runner.invoke(_app, [])

        assert result.exit_code == 0, result.output
        assert _remote_feature(stood_in) == run_git(stood_in, "rev-parse", "HEAD")
        assert not _remote_feature(inherited)

    def test_a_relative_repo_resolves_from_where_the_operator_stood(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _clone_on_feature(tmp_path / "checkouts")
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(tmp_path / "checkouts"))

        result = runner.invoke(_app, ["--repo", "clone"])

        assert result.exit_code == 0, result.output
        assert _remote_feature(clone) == run_git(clone, "rev-parse", "HEAD")
