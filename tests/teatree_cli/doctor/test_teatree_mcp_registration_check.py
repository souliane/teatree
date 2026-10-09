"""``_check_teatree_mcp_registration`` — the `t3 doctor` own-server gate (#2863)."""

from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.checks_mcp import _check_teatree_mcp_registration


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

    def test_a_well_formed_registration_is_ok_silent(self, tmp_path: Path, capsys) -> None:
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}}')
        with patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=tmp_path):
            assert _check_teatree_mcp_registration() is True
        assert capsys.readouterr().out == ""

    def test_a_registration_with_the_wrong_command_warns(self, tmp_path: Path, capsys) -> None:
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"teatree": {"command": "t3", "args": ["mcp"]}}}')
        with patch("teatree.cli.doctor.plugin_repair._resolve_main_clone", return_value=tmp_path):
            assert _check_teatree_mcp_registration() is True
        out = capsys.readouterr().out
        assert out.startswith("WARN")
        assert "expected 't3' ['mcp', 'serve']" in out
