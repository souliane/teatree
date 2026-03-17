"""Tests for t3 CLI high-level commands (start-ticket, ship, daily, full-status, post-evidence)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from t3_cli import daily, full_status, post_evidence, ship, start_ticket


class TestFullStatus:
    def test_shows_all_states_json(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        with (
            patch("t3_cli.detect_ticket_dir", return_value=str(tmp_path)),
            patch("t3_cli.WorktreeLifecycle") as wt_cls,
            patch("t3_cli.TicketLifecycle") as tk_cls,
            patch("t3_cli._get_session") as mock_session,
        ):
            wt_cls.return_value.state = "ready"
            tk_cls.return_value.state = "coded"
            mock_session.return_value.state = "testing"
            full_status(as_json=True)

        data = json.loads(capsys.readouterr().out)
        assert data["worktree"] == "ready"
        assert data["ticket"] == "coded"
        assert data["session"] == "testing"

    def test_shows_na_when_not_in_ticket(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("t3_cli.detect_ticket_dir", return_value=""),
            patch("t3_cli._get_session") as mock_session,
        ):
            mock_session.return_value.state = "idle"
            full_status(as_json=False)

        out = capsys.readouterr().out
        assert "n/a" in out

    def test_text_output(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        with (
            patch("t3_cli.detect_ticket_dir", return_value=str(tmp_path)),
            patch("t3_cli.WorktreeLifecycle") as wt_cls,
            patch("t3_cli.TicketLifecycle") as tk_cls,
            patch("t3_cli._get_session") as mock_session,
        ):
            wt_cls.return_value.state = "provisioned"
            tk_cls.return_value.state = "started"
            mock_session.return_value.state = "coding"
            full_status(as_json=False)

        out = capsys.readouterr().out
        assert "provisioned" in out
        assert "coding" in out


class TestStartTicket:
    def test_chains_all_steps(self) -> None:
        lc = MagicMock(state="created")

        def set_provisioned(*_a: object, **_kw: object) -> None:
            lc.state = "provisioned"

        def set_services_up() -> None:
            lc.state = "services_up"

        def set_ready() -> None:
            lc.state = "ready"

        lc.provision.side_effect = set_provisioned
        lc.start_services.side_effect = set_services_up
        lc.verify.side_effect = set_ready
        lc.status.return_value = {"state": "ready", "facts": {}}

        tk = MagicMock(state="not_started")

        def tk_scope(**_kw: object) -> None:
            tk.state = "scoped"

        def tk_start(**_kw: object) -> None:
            tk.state = "started"

        tk.scope.side_effect = tk_scope
        tk.start.side_effect = tk_start
        tk.status.return_value = {"state": "started", "facts": {}}

        with (
            patch("t3_cli.ep_call"),
            patch("t3_cli._get_lifecycle", return_value=lc),
            patch("t3_cli.resolve_context", return_value=MagicMock(wt_dir="/tmp/wt", main_repo="/tmp/repo")),
            patch("t3_cli._get_ticket", return_value=tk),
        ):
            start_ticket(issue_url="https://example.com/issue/1", variant="customer")

        lc.provision.assert_called_once()
        tk.scope.assert_called_once()

    def test_skips_provision_when_already_provisioned(self) -> None:
        lc = MagicMock(state="provisioned")
        lc.start_services.side_effect = lambda: setattr(lc, "state", "services_up")
        lc.verify.side_effect = lambda: setattr(lc, "state", "ready")
        lc.status.return_value = {"state": "ready", "facts": {}}

        tk = MagicMock(state="scoped")
        tk.start.side_effect = lambda **_kw: setattr(tk, "state", "started")
        tk.status.return_value = {"state": "started", "facts": {}}

        with (
            patch("t3_cli.ep_call"),
            patch("t3_cli._get_lifecycle", return_value=lc),
            patch("t3_cli.resolve_context", return_value=MagicMock(wt_dir="/tmp/wt", main_repo="/tmp/repo")),
            patch("t3_cli._get_ticket", return_value=tk),
        ):
            start_ticket(issue_url="https://example.com/1", variant="")

        lc.provision.assert_not_called()  # already provisioned
        lc.start_services.assert_called_once()

    def test_skips_all_when_already_ready(self) -> None:
        lc = MagicMock(state="ready")
        lc.status.return_value = {"state": "ready", "facts": {}}

        tk = MagicMock(state="started")
        tk.status.return_value = {"state": "started", "facts": {}}

        with (
            patch("t3_cli.ep_call"),
            patch("t3_cli._get_lifecycle", return_value=lc),
            patch("t3_cli.resolve_context", return_value=MagicMock(wt_dir="/tmp/wt", main_repo="/tmp/repo")),
            patch("t3_cli._get_ticket", return_value=tk),
        ):
            start_ticket(issue_url="https://example.com/1", variant="")

        lc.provision.assert_not_called()
        lc.start_services.assert_not_called()
        tk.scope.assert_not_called()
        tk.start.assert_not_called()


class TestShip:
    def test_blocks_without_testing(self) -> None:
        session = MagicMock()
        session.has_visited.return_value = False
        with patch("t3_cli._get_session", return_value=session), pytest.raises(typer.Exit):
            ship(force=False)

    def test_force_overrides_gates(self) -> None:
        session = MagicMock()
        session.has_visited.return_value = False
        tk = MagicMock(state="reviewed")
        tk.ship.side_effect = lambda **_kw: setattr(tk, "state", "shipped")

        with (
            patch("t3_cli._get_session", return_value=session),
            patch("t3_cli.ep_call"),
            patch("t3_cli._get_ticket", return_value=tk),
        ):
            ship(force=True)

        session.begin_shipping.assert_called_once_with(force=True)

    def test_passes_when_all_gates_met(self) -> None:
        session = MagicMock()
        session.has_visited.return_value = True
        tk = MagicMock(state="reviewed")
        tk.ship.side_effect = lambda **_kw: setattr(tk, "state", "shipped")

        with (
            patch("t3_cli._get_session", return_value=session),
            patch("t3_cli.ep_call"),
            patch("t3_cli._get_ticket", return_value=tk),
        ):
            ship(force=False)


class TestDaily:
    def test_calls_all_steps(self) -> None:
        with patch("t3_cli.ep_call") as mock_ep:
            daily()

        calls = [c[0][0] for c in mock_ep.call_args_list]
        assert "followup_collect" in calls
        assert "followup_check_gates" in calls
        assert "followup_remind_reviewers" in calls


class TestPostEvidence:
    def test_delegates_to_ep(self) -> None:
        with patch("t3_cli.ep_call") as mock_ep:
            post_evidence(paths=["screenshot.png"], mr_url="https://x", title="Evidence")

        mock_ep.assert_called_once_with("wt_post_mr_evidence", ["screenshot.png"], "https://x", "Evidence")
