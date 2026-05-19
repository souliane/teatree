"""Tests for ``t3 loop dashboard`` — tabular per-tick fleet view (#1005)."""

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.loop import loop_app

runner = CliRunner()


def _seed(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class TestDashboardCommand:
    def test_prints_to_stdout_by_default(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "tick-actions.jsonl"
        _seed(
            sidecar,
            [
                {
                    "overlay": "acme",
                    "ref": "!42",
                    "label": "Fix the thing",
                    "url": "https://gitlab.example/acme/-/merge_requests/42",
                    "action_kind": "statusline",
                    "scanner": "MyPrsScanner",
                },
            ],
        )

        result = runner.invoke(loop_app, ["dashboard", "--source", str(sidecar)])

        assert result.exit_code == 0
        assert "## [acme]" in result.stdout
        assert "!42" in result.stdout

    def test_invalid_format_exits_with_error(self, tmp_path: Path) -> None:
        result = runner.invoke(loop_app, ["dashboard", "--format", "html", "--source", str(tmp_path / "x.jsonl")])
        assert result.exit_code == 2
        assert "Invalid --format" in result.stdout

    def test_send_to_slack_invokes_notify_user(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "tick-actions.jsonl"
        _seed(
            sidecar,
            [
                {
                    "overlay": "acme",
                    "ref": "!42",
                    "label": "Fix",
                    "url": "https://gitlab.example/acme/-/merge_requests/42",
                    "action_kind": "statusline",
                    "scanner": "MyPrsScanner",
                },
            ],
        )

        with patch("teatree.notify.notify_user", return_value=True) as notify_mock:
            result = runner.invoke(
                loop_app,
                ["dashboard", "--send-to-slack", "--source", str(sidecar)],
            )

        assert result.exit_code == 0
        notify_mock.assert_called_once()
        # The Slack send uses Slack mrkdwn — angle-bracket pipe-link form.
        sent_text = notify_mock.call_args.args[0]
        assert "<https://gitlab.example/acme/-/merge_requests/42|!42>" in sent_text
        assert notify_mock.call_args.kwargs["idempotency_key"].startswith("dashboard-")

    def test_send_to_slack_failure_exits_nonzero(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "tick-actions.jsonl"
        _seed(sidecar, [{"overlay": "x", "ref": "#1", "label": "y", "url": "", "action_kind": "statusline"}])
        with patch("teatree.notify.notify_user", return_value=False):
            result = runner.invoke(
                loop_app,
                ["dashboard", "--send-to-slack", "--source", str(sidecar)],
            )

        assert result.exit_code == 1
        assert "notify_user returned False" in result.stdout

    def test_self_dm_marker_renders_this_dm_label(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "tick-actions.jsonl"
        _seed(
            sidecar,
            [
                {
                    "overlay": "acme",
                    "ref": "dm",
                    "label": "delivered",
                    "url": "",
                    "action_kind": "slack_dm",
                    "scanner": "notify_user",
                },
            ],
        )
        result = runner.invoke(loop_app, ["dashboard", "--source", str(sidecar), "--self-dm-marker"])

        assert result.exit_code == 0
        assert "(this DM)" in result.stdout
