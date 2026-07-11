"""``_check_teatree_mcp_registration`` — the `t3 doctor` own-server gate (#2863)."""

from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.checks import _check_teatree_mcp_registration
from teatree.core.mcp_connectivity import McpServerStatus


class TestTeatreeMcpRegistrationDoctorCheck:
    def test_no_resolvable_repo_is_ok_silent(self, capsys) -> None:
        with patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=None):
            assert _check_teatree_mcp_registration() is True
        assert capsys.readouterr().out == ""

    def test_resolver_crash_degrades_to_warn(self, capsys) -> None:
        with patch(
            "teatree.cli.doctor.plugin_repair._resolve_main_clone",
            side_effect=RuntimeError("boom"),
        ):
            assert _check_teatree_mcp_registration() is True
        assert "WARN" in capsys.readouterr().out

    def test_missing_mcp_json_warns_never_fails(self, tmp_path: Path, capsys) -> None:
        """A WARN, not a FAIL — a lagging main clone is normal, self-correcting state."""
        with patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=tmp_path):
            assert _check_teatree_mcp_registration() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "FAIL" not in out
        assert "does not declare" in out

    def test_registered_and_connected_is_ok_silent(self, tmp_path: Path, capsys) -> None:
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}}')
        connected = [McpServerStatus(name="teatree", url="", connected=True)]
        with (
            patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=tmp_path),
            patch("teatree.core.mcp_connectivity.probe_mcp_servers", return_value=connected),
        ):
            assert _check_teatree_mcp_registration() is True
        assert capsys.readouterr().out == ""

    def test_registered_but_disconnected_warns(self, tmp_path: Path, capsys) -> None:
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}}')
        disconnected = [McpServerStatus(name="teatree", url="", connected=False)]
        with (
            patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=tmp_path),
            patch("teatree.core.mcp_connectivity.probe_mcp_servers", return_value=disconnected),
        ):
            assert _check_teatree_mcp_registration() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "not" in out

    def test_registered_but_absent_from_probe_is_silent(self, tmp_path: Path, capsys) -> None:
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}}')
        with (
            patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=tmp_path),
            patch("teatree.core.mcp_connectivity.probe_mcp_servers", return_value=[]),
        ):
            assert _check_teatree_mcp_registration() is True
        assert capsys.readouterr().out == ""

    def test_probe_failure_is_ok_best_effort(self, tmp_path: Path, capsys) -> None:
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}}')
        with (
            patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=tmp_path),
            patch("teatree.core.mcp_connectivity.probe_mcp_servers", side_effect=FileNotFoundError("no claude")),
        ):
            assert _check_teatree_mcp_registration() is True
        assert capsys.readouterr().out == ""
