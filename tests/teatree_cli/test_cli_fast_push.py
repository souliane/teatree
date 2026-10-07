"""Tests for the top-level ``t3 fast-push`` CLI command (delegates to the engine)."""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.fast_push import fast_push
from teatree.core.invocation_cwd import INVOCATION_CWD_ENV
from teatree.core.push.fast_push import (
    EMPTY_DELTA_PR_SKIP,
    LEAK_GATES,
    UNAPPROVABLE_AUTHOR_PR_REFUSAL,
    FastPushOutcome,
    LeakFinding,
)
from tests._git_repo import make_git_repo

runner = CliRunner()

_app = typer.Typer()
_app.command()(fast_push)


def _refusal() -> FastPushOutcome:
    return FastPushOutcome(
        ok=False,
        branch="feature",
        executed_gates=LEAK_GATES,
        findings=[LeakFinding(gate="banned-terms", path="notes.md", detail="banned term 'x'")],
    )


def _success() -> FastPushOutcome:
    return FastPushOutcome(
        ok=True,
        branch="feature",
        executed_gates=LEAK_GATES,
        committed=True,
        pushed=True,
        pr_url="https://example.invalid/pr/1",
        pr_action="created",
        message="feat: x",
    )


class TestFastPushCommand:
    def test_refusal_prints_findings_and_exits_1(self) -> None:
        with patch("teatree.cli.fast_push.FastPusher") as pusher:
            pusher.return_value.run.return_value = _refusal()
            result = runner.invoke(_app, [])
        assert result.exit_code == 1
        assert "REFUSED" in result.output
        assert "[banned-terms] notes.md: banned term 'x'" in result.output

    def test_success_prints_pr_url(self) -> None:
        with patch("teatree.cli.fast_push.FastPusher") as pusher:
            pusher.return_value.run.return_value = _success()
            result = runner.invoke(_app, ["-m", "feat: x", "--remaining", "wire docs"])
        assert result.exit_code == 0
        assert "PR created: https://example.invalid/pr/1" in result.output
        assert pusher.call_args.kwargs["message"] == "feat: x"
        assert pusher.call_args.kwargs["remaining"] == "wire docs"

    def test_json_output(self) -> None:
        with patch("teatree.cli.fast_push.FastPusher") as pusher:
            pusher.return_value.run.return_value = _success()
            result = runner.invoke(_app, ["--json"])
        assert result.exit_code == 0
        assert '"pr_action": "created"' in result.output

    def test_an_empty_delta_skip_prints_its_reason(self) -> None:
        """The remedy is only useful if it reaches the operator — no `pr_url`, so no other arm prints it."""
        outcome = _success()
        outcome.pr_url = ""
        outcome.pr_action = EMPTY_DELTA_PR_SKIP
        outcome.pr_skip_reason = "branch 'feature' carries no changes over origin/main"

        with patch("teatree.cli.fast_push.FastPusher") as pusher:
            pusher.return_value.run.return_value = outcome
            result = runner.invoke(_app, [])

        assert result.exit_code == 0
        assert "PR skipped: branch 'feature' carries no changes over origin/main" in result.output

    def test_a_refused_author_prints_its_reason_and_exits_1_though_the_push_landed(self) -> None:
        """A branch left with no MR is an unfinished delivery, so the command cannot report success."""
        outcome = _success()
        outcome.pr_url = ""
        outcome.pr_action = UNAPPROVABLE_AUTHOR_PR_REFUSAL
        outcome.pr_skip_reason = "git@gitlab.com:org/group/factory.git would be authored by the owner"

        with patch("teatree.cli.fast_push.FastPusher") as pusher:
            pusher.return_value.run.return_value = outcome
            result = runner.invoke(_app, [])

        assert result.exit_code == 1
        assert "PR REFUSED (the push landed): git@gitlab.com:org/group/factory.git would be authored" in result.output


class TestFastPushActsWhereTheOperatorStood:
    def test_the_default_repo_is_the_declared_checkout_not_the_container_workdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = make_git_repo(tmp_path / "checkout")
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(checkout))

        with patch("teatree.cli.fast_push.FastPusher") as pusher:
            pusher.return_value.run.return_value = _success()
            result = runner.invoke(_app, [])

        assert result.exit_code == 0, result.output
        assert pusher.call_args.kwargs["repo"] == checkout.resolve()
