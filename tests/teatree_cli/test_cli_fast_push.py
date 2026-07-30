"""Tests for the top-level ``t3 fast-push`` CLI command (delegates to the engine)."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.fast_push import fast_push
from teatree.core.fast_push import LEAK_GATES, FastPushOutcome, LeakFinding

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
