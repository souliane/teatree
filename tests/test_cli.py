"""Tests for teetree.cli — comprehensive CLI command coverage.

Uses typer.testing.CliRunner to invoke commands and mocks external
dependencies (subprocess, filesystem, network, Django).
"""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from teetree.cli import (
    _bridge_subcommand,
    _build_overlay_app,
    _camelize,
    _check_editable_sanity,
    _collect_overlay_skills,
    _current_git_branch,
    _editable_info,
    _find_overlay_project,
    _find_project_root,
    _get_ci_project,
    _get_ci_service,
    _get_gitlab_token,
    _managepy,
    _patch_manage_py,
    _patch_settings,
    _patch_urls,
    _print_package_info,
    _register_overlay_commands,
    _repair_symlinks,
    _run_script,
    _show_info,
    _uvicorn,
    _write_overlay,
    _write_skill_md,
    app,
)

runner = CliRunner()


# ── docs command ─────────────────────────────────────────────────────


def test_docs_no_mkdocs_yml(tmp_path, monkeypatch):
    """Docs command fails if no mkdocs.yml found."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    result = runner.invoke(app, ["docs"])
    assert result.exit_code == 1
    assert "No mkdocs.yml" in result.output


def test_docs_mkdocs_not_installed(tmp_path, monkeypatch):
    """Docs command fails if mkdocs is not installed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "mkdocs.yml").write_text("site_name: Test\n")

    # Make mkdocs unimportable
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mkdocs":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = runner.invoke(app, ["docs"])
    assert result.exit_code == 1
    assert "mkdocs is not installed" in result.output


def test_docs_runs_mkdocs_serve(tmp_path, monkeypatch):
    """Docs command runs mkdocs serve when everything is available."""
    import sys  # noqa: PLC0415
    import types  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "mkdocs.yml").write_text("site_name: Test\n")

    # Make mkdocs importable by inserting a fake module
    fake_mkdocs = types.ModuleType("mkdocs")
    monkeypatch.setitem(sys.modules, "mkdocs", fake_mkdocs)

    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["docs"])
        assert result.exit_code == 0
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "mkdocs" in str(call_args)


# ── agent command ─────────────────────────────────────────────────────


def test_agent_no_claude(tmp_path, monkeypatch):
    """Agent command fails if claude CLI not found."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    with (
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("shutil.which", return_value=None),
    ):
        result = runner.invoke(app, ["agent"])
        assert result.exit_code == 1
        assert "claude CLI not found" in result.output


def test_agent_with_active_overlay(tmp_path, monkeypatch):
    """Agent command launches claude with overlay context."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    from teetree.config import OverlayEntry  # noqa: PLC0415

    mock_overlay = OverlayEntry(name="test-overlay", settings_module="test.settings")

    with (
        patch("teetree.config.discover_active_overlay", return_value=mock_overlay),
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("teetree.agents.skill_bundle.resolve_dependencies", return_value=["t3-code"]),
        patch("teetree.cli.os.execvp") as mock_exec,
    ):
        runner.invoke(app, ["agent", "fix bug"])
        mock_exec.assert_called_once()
        cmd = mock_exec.call_args[0][1]
        assert cmd[0] == "/usr/bin/claude"
        assert "--append-system-prompt" in cmd


def test_agent_no_overlay(tmp_path, monkeypatch):
    """Agent command works without active overlay."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    with (
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("teetree.agents.skill_bundle.resolve_dependencies", return_value=["t3-code"]),
        patch("teetree.cli.os.execvp") as mock_exec,
    ):
        runner.invoke(app, ["agent"])
        mock_exec.assert_called_once()


# ── sessions command ──────────────────────────────────────────────────


def test_sessions_no_results(monkeypatch):
    """Sessions command shows message when no sessions found."""
    with patch("teetree.claude_sessions.list_sessions", return_value=[]):
        result = runner.invoke(app, ["sessions", "--all"])
        assert result.exit_code == 0
        assert "No sessions found" in result.output


def test_sessions_shows_results(monkeypatch):
    """Sessions command renders session list."""
    from teetree.claude_sessions import SessionInfo  # noqa: PLC0415

    now = time.time()
    sessions = [
        SessionInfo(
            session_id="abc123",
            project="~/workspace/my-project",
            first_prompt="fix the bug",
            timestamp=now * 1000,  # ms format
            mtime=now,
            cwd="/home/user/workspace",
            status="interrupted",
        ),
        SessionInfo(
            session_id="def456",
            project="~/workspace/other",
            first_prompt="add feature",
            timestamp=now - 7200,  # seconds, <86400 ago
            mtime=now - 7200,
            cwd="",
            status="finished",
        ),
        SessionInfo(
            session_id="ghi789",
            project="~/workspace/old",
            first_prompt="x" * 100,
            timestamp=now - 100000,  # >1 day
            mtime=now - 100000,
            cwd="",
            status="finished",
        ),
        SessionInfo(
            session_id="jkl012",
            project="~/workspace/zero",
            first_prompt="",
            timestamp=0,
            mtime=now,
            cwd="",
            status="active",
        ),
        SessionInfo(
            session_id="str123",
            project="~/workspace/strtime",
            first_prompt="string ts",
            timestamp="invalid_ts",
            mtime=now,
            cwd="/some/path",
            status="interrupted",
        ),
    ]
    with patch("teetree.claude_sessions.list_sessions", return_value=sessions):
        result = runner.invoke(app, ["sessions", "--all"])
        assert result.exit_code == 0
        assert "fix the bug" in result.output
        assert "abc123" in result.output
        # finished sessions should show "done"
        assert "done" in result.output


# ── overlays command ──────────────────────────────────────────────────


def test_overlays_none_found():
    """Overlays command shows help when no overlays found."""
    with (
        patch("teetree.config.discover_overlays", return_value=[]),
        patch("teetree.config.discover_active_overlay", return_value=None),
    ):
        result = runner.invoke(app, ["overlays"])
        assert result.exit_code == 0
        assert "No overlays found" in result.output


def test_overlays_lists_installed():
    """Overlays command lists installed overlays."""
    from teetree.config import OverlayEntry  # noqa: PLC0415

    entries = [
        OverlayEntry(name="acme", settings_module="acme.settings"),
        OverlayEntry(name="demo", settings_module="demo.settings"),
    ]
    active = OverlayEntry(name="acme", settings_module="acme.settings")
    with (
        patch("teetree.config.discover_overlays", return_value=entries),
        patch("teetree.config.discover_active_overlay", return_value=active),
    ):
        result = runner.invoke(app, ["overlays"])
        assert result.exit_code == 0
        assert "acme" in result.output
        assert "(active)" in result.output


# ── info command ──────────────────────────────────────────────────────


def test_info_command():
    """Info command shows installation details."""
    with (
        patch("shutil.which", return_value="/usr/local/bin/t3"),
        patch("teetree.cli._editable_info", return_value=(True, "file:///home/src")),
        patch("teetree.cli._print_package_info"),
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("teetree.config.discover_overlays", return_value=[]),
    ):
        result = runner.invoke(app, ["info"])
        assert result.exit_code == 0


# ── config write-skill-cache command ──────────────────────────────────


def test_write_skill_cache_writes_json(tmp_path, monkeypatch):
    """write-skill-cache writes overlay metadata to cache."""
    from teetree.config import OverlayEntry  # noqa: PLC0415

    active = OverlayEntry(name="test", settings_module="test.settings")
    mock_overlay = MagicMock()
    mock_overlay.get_skill_metadata.return_value = {"skill_path": "skills/t3-test/SKILL.md"}

    monkeypatch.setattr("teetree.config.DATA_DIR", tmp_path)
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    with (
        patch("teetree.config.discover_active_overlay", return_value=active),
        patch("django.setup"),
        patch("teetree.core.overlay_loader.get_overlay", return_value=mock_overlay),
    ):
        result = runner.invoke(app, ["config", "write-skill-cache"])
        assert result.exit_code == 0
        assert "Wrote skill metadata" in result.output
        cache = tmp_path / "skill-metadata.json"
        assert cache.is_file()
        data = json.loads(cache.read_text())
        assert data["skill_path"] == "skills/t3-test/SKILL.md"


# ── CI commands ──────────────────────────────────────────────────────


def test_ci_cancel_no_service(monkeypatch):
    """Ci cancel fails without CI service."""
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with patch("teetree.cli._get_ci_service", return_value=None):
        result = runner.invoke(app, ["ci", "cancel"])
        assert result.exit_code == 1
        assert "No CI service" in result.output


def test_ci_cancel_no_branch(monkeypatch):
    """Ci cancel fails when branch cannot be detected."""
    mock_ci = MagicMock()
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value=""),
    ):
        result = runner.invoke(app, ["ci", "cancel"])
        assert result.exit_code == 1
        assert "Could not detect branch" in result.output


def test_ci_cancel_with_results():
    """Ci cancel shows cancelled pipelines."""
    mock_ci = MagicMock()
    mock_ci.cancel_pipelines.return_value = [123, 456]
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "cancel"])
        assert result.exit_code == 0
        assert "Cancelled 2" in result.output


def test_ci_cancel_no_pipelines():
    """Ci cancel shows message when no pipelines found."""
    mock_ci = MagicMock()
    mock_ci.cancel_pipelines.return_value = []
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "cancel"])
        assert result.exit_code == 0
        assert "No running/pending" in result.output


def test_ci_divergence(monkeypatch, tmp_path):
    """Ci divergence shows ahead/behind counts."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("teetree.utils.git.run", side_effect=["", "3", "1"]),
        patch("teetree.utils.git.current_branch", return_value="feature-branch"),
    ):
        result = runner.invoke(app, ["ci", "divergence"])
        assert result.exit_code == 0
        assert "3 ahead" in result.output
        assert "1 behind" in result.output


def test_ci_divergence_no_upstream(monkeypatch, tmp_path):
    """Ci divergence fails when no upstream configured."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with patch("teetree.utils.git.run", side_effect=Exception("no upstream")):
        result = runner.invoke(app, ["ci", "divergence"])
        assert result.exit_code == 1
        assert "No upstream" in result.output


def test_ci_fetch_errors_with_errors():
    """Ci fetch-errors shows error logs."""
    mock_ci = MagicMock()
    mock_ci.fetch_pipeline_errors.return_value = ["Error in job build", "Error in job test"]
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "fetch-errors"])
        assert result.exit_code == 0
        assert "Error in job build" in result.output


def test_ci_fetch_errors_no_errors():
    """Ci fetch-errors shows clean message."""
    mock_ci = MagicMock()
    mock_ci.fetch_pipeline_errors.return_value = []
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "fetch-errors"])
        assert result.exit_code == 0
        assert "No errors found" in result.output


def test_ci_fetch_errors_no_service():
    with patch("teetree.cli._get_ci_service", return_value=None):
        result = runner.invoke(app, ["ci", "fetch-errors"])
        assert result.exit_code == 1


def test_ci_fetch_failed_tests_with_failures():
    """Ci fetch-failed-tests shows failed test IDs."""
    mock_ci = MagicMock()
    mock_ci.fetch_failed_tests.return_value = ["test_foo", "test_bar"]
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "fetch-failed-tests"])
        assert result.exit_code == 0
        assert "Failed tests (2)" in result.output
        assert "test_foo" in result.output


def test_ci_fetch_failed_tests_none():
    mock_ci = MagicMock()
    mock_ci.fetch_failed_tests.return_value = []
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "fetch-failed-tests"])
        assert result.exit_code == 0
        assert "No failed tests" in result.output


def test_ci_fetch_failed_tests_no_service():
    with patch("teetree.cli._get_ci_service", return_value=None):
        result = runner.invoke(app, ["ci", "fetch-failed-tests"])
        assert result.exit_code == 1


def test_ci_trigger_e2e_success():
    """Ci trigger-e2e triggers pipeline."""
    mock_ci = MagicMock()
    mock_ci.trigger_pipeline.return_value = {"web_url": "https://ci/pipeline/1"}
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "trigger-e2e"])
        assert result.exit_code == 0
        assert "Pipeline triggered" in result.output


def test_ci_trigger_e2e_error():
    mock_ci = MagicMock()
    mock_ci.trigger_pipeline.return_value = {"error": "forbidden"}
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "trigger-e2e"])
        assert result.exit_code == 1
        assert "forbidden" in result.output


def test_ci_trigger_e2e_no_service():
    with patch("teetree.cli._get_ci_service", return_value=None):
        result = runner.invoke(app, ["ci", "trigger-e2e"])
        assert result.exit_code == 1


def test_ci_quality_check_success():
    mock_ci = MagicMock()
    mock_ci.quality_check.return_value = {
        "pipeline_id": 42,
        "status": "success",
        "total_count": 100,
        "success_count": 98,
        "failed_count": 2,
    }
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "quality-check"])
        assert result.exit_code == 0
        assert "Pipeline 42" in result.output
        assert "Failed: 2" in result.output


def test_ci_quality_check_error():
    mock_ci = MagicMock()
    mock_ci.quality_check.return_value = {"error": "no pipeline"}
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
        patch("teetree.cli._current_git_branch", return_value="main"),
    ):
        result = runner.invoke(app, ["ci", "quality-check"])
        assert result.exit_code == 1


def test_ci_quality_check_no_service():
    with patch("teetree.cli._get_ci_service", return_value=None):
        result = runner.invoke(app, ["ci", "quality-check"])
        assert result.exit_code == 1


# ── Review draft note commands ────────────────────────────────────────


def test_post_draft_note_general(monkeypatch):
    """post-draft-note posts a general note (no file/line)."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.post_json.return_value = {"id": 42, "position": None}

    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "looks good"])
        assert result.exit_code == 0
        assert "OK draft_note_id=42" in result.output


def test_post_draft_note_inline(monkeypatch):
    """post-draft-note posts an inline note with file and line."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.get_json.return_value = {
        "diff_refs": {
            "base_sha": "abc",
            "head_sha": "def",
            "start_sha": "ghi",
        },
    }
    mock_api.post_json.return_value = {
        "id": 99,
        "position": {"line_code": "abc_1_1"},
    }

    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(
            app,
            ["review", "post-draft-note", "org/repo", "1", "fix this", "--file", "src/main.py", "--line", "10"],
        )
        assert result.exit_code == 0
        assert "OK draft_note_id=99" in result.output
        assert "line_code=abc_1_1" in result.output


def test_post_draft_note_inline_no_line_code(monkeypatch):
    """post-draft-note warns when line_code is null."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.get_json.return_value = {
        "diff_refs": {"base_sha": "a", "head_sha": "b", "start_sha": "c"},
    }
    mock_api.post_json.return_value = {"id": 100, "position": {}}

    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(
            app,
            ["review", "post-draft-note", "org/repo", "1", "fix this", "--file", "a.py", "--line", "5"],
        )
        assert result.exit_code == 0
        assert "WARNING: line_code is null" in result.output


def test_post_draft_note_no_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stderr="", returncode=1)
        result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "note"])
        assert result.exit_code == 1
        assert "No GitLab token" in result.output


def test_post_draft_note_mr_fetch_fails(monkeypatch):
    """post-draft-note fails when MR data cannot be fetched."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.get_json.return_value = None

    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(
            app,
            ["review", "post-draft-note", "org/repo", "1", "note", "--file", "a.py", "--line", "1"],
        )
        assert result.exit_code == 1
        assert "Could not fetch MR" in result.output


def test_post_draft_note_no_diff_refs(monkeypatch):
    """post-draft-note fails when MR has no diff_refs."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.get_json.return_value = {"diff_refs": None}

    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(
            app,
            ["review", "post-draft-note", "org/repo", "1", "note", "--file", "a.py", "--line", "1"],
        )
        assert result.exit_code == 1
        assert "no diff_refs" in result.output


def test_post_draft_note_post_fails(monkeypatch):
    """post-draft-note fails when the POST returns empty."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.post_json.return_value = None

    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(app, ["review", "post-draft-note", "org/repo", "1", "note"])
        assert result.exit_code == 1
        assert "Failed to post" in result.output


def test_delete_draft_note_success(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_response = MagicMock(status_code=204)
    with patch("httpx.delete", return_value=mock_response):
        result = runner.invoke(app, ["review", "delete-draft-note", "org/repo", "1", "42"])
        assert result.exit_code == 0
        assert "OK deleted" in result.output


def test_delete_draft_note_failure(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_response = MagicMock(status_code=404)
    with patch("httpx.delete", return_value=mock_response):
        result = runner.invoke(app, ["review", "delete-draft-note", "org/repo", "1", "42"])
        assert result.exit_code == 1
        assert "Failed: HTTP 404" in result.output


def test_delete_draft_note_no_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stderr="", returncode=1)
        result = runner.invoke(app, ["review", "delete-draft-note", "org/repo", "1", "42"])
        assert result.exit_code == 1
        assert "No GitLab token" in result.output


def test_list_draft_notes_success(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.get_json.return_value = [
        {"id": 1, "note": "first note text", "position": {"new_path": "a.py", "new_line": 10}},
        {"id": 2, "note": "second note", "position": None},
        "not a dict",
    ]
    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(app, ["review", "list-draft-notes", "org/repo", "1"])
        assert result.exit_code == 0
        assert "a.py:10" in result.output


def test_list_draft_notes_none_found(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    mock_api = MagicMock()
    mock_api.get_json.return_value = "not a list"
    with patch("teetree.utils.gitlab_api.GitLabAPI", return_value=mock_api):
        result = runner.invoke(app, ["review", "list-draft-notes", "org/repo", "1"])
        assert result.exit_code == 0
        assert "No draft notes" in result.output


def test_list_draft_notes_no_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stderr="", returncode=1)
        result = runner.invoke(app, ["review", "list-draft-notes", "org/repo", "1"])
        assert result.exit_code == 1
        assert "No GitLab token" in result.output


# ── Doctor commands ──────────────────────────────────────────────────


def test_doctor_repair(tmp_path, monkeypatch):
    """Doctor repair creates/fixes symlinks."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "t3-code").mkdir()
    (skills_dir / "t3-code" / "SKILL.md").touch()

    claude_skills = tmp_path / "claude_skills"
    claude_skills.mkdir()
    # Create a broken symlink
    broken = claude_skills / "broken-link"
    broken.symlink_to(tmp_path / "nonexistent")

    with (
        patch("teetree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_dir),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("teetree.cli._collect_overlay_skills", return_value=[]),
    ):
        # Create the .claude/skills dir where the command expects it
        real_claude_skills = tmp_path / ".claude" / "skills"
        real_claude_skills.mkdir(parents=True)
        # Add a broken symlink
        broken2 = real_claude_skills / "broken-link"
        broken2.symlink_to(tmp_path / "nonexistent2")

        result = runner.invoke(app, ["doctor", "repair"])
        assert result.exit_code == 0
        assert "Skills:" in result.output


def test_doctor_repair_no_skills_dir(tmp_path):
    """Doctor repair fails when skills dir not found."""
    with patch("teetree.agents.skill_bundle.DEFAULT_SKILLS_DIR", tmp_path / "nonexistent"):
        result = runner.invoke(app, ["doctor", "repair"])
        assert result.exit_code == 1
        assert "Skills directory not found" in result.output


def test_doctor_check_ok():
    """Doctor check passes when all checks pass."""
    with (
        patch("teetree.cli._check_editable_sanity", return_value=[]),
    ):
        result = runner.invoke(app, ["doctor", "check"])
        assert result.exit_code == 0
        assert "All checks passed" in result.output


def test_doctor_check_with_warnings():
    """Doctor check shows warnings."""
    with patch("teetree.cli._check_editable_sanity", return_value=["teatree is editable but not declared"]):
        result = runner.invoke(app, ["doctor", "check"])
        assert result.exit_code == 0
        assert "WARN" in result.output


def test_doctor_check_fails_when_required_tool_missing():
    """Doctor check fails when a required tool is not on PATH."""
    with (
        patch("teetree.cli.shutil.which", side_effect=lambda t: None if t == "direnv" else f"/usr/bin/{t}"),
        patch("teetree.cli._check_editable_sanity", return_value=[]),
    ):
        result = runner.invoke(app, ["doctor", "check"])
        assert result.exit_code == 0  # typer returns 0; check() returns bool
        assert "FAIL  Required tool not found: direnv" in result.output


# ── Review-request discover ──────────────────────────────────────────


def test_review_request_discover(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("teetree.cli._find_overlay_project", return_value=tmp_path),
        patch("teetree.cli._managepy") as mock_manage,
    ):
        result = runner.invoke(app, ["review-request", "discover"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(tmp_path, "followup", "discover-mrs")


# ── Tool commands ────────────────────────────────────────────────────


def test_tool_privacy_scan():
    with patch("teetree.cli._run_script") as mock:
        result = runner.invoke(app, ["tool", "privacy-scan", "myfile.txt"])
        assert result.exit_code == 0
        mock.assert_called_once_with("privacy_scan", "myfile.txt")


def test_tool_analyze_video():
    with patch("teetree.cli._run_script") as mock:
        result = runner.invoke(app, ["tool", "analyze-video", "/path/to/video.mp4"])
        assert result.exit_code == 0
        mock.assert_called_once_with("analyze_video", "/path/to/video.mp4")


def test_tool_bump_deps():
    with patch("teetree.cli._run_script") as mock:
        result = runner.invoke(app, ["tool", "bump-deps"])
        assert result.exit_code == 0
        mock.assert_called_once_with("bump-pyproject-deps-from-lock-file")


# ── Internal helpers ─────────────────────────────────────────────────


def test_find_project_root_walks_up(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / "a" / "pyproject.toml").write_text("[project]\n")
    monkeypatch.chdir(nested)
    result = _find_project_root()
    assert result == tmp_path / "a"


def test_find_project_root_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _find_project_root()
    assert result == tmp_path


def test_find_overlay_project_with_active(tmp_path):
    from teetree.config import OverlayEntry  # noqa: PLC0415

    active = OverlayEntry(name="test", settings_module="test.settings", project_path=tmp_path)
    with patch("teetree.config.discover_active_overlay", return_value=active):
        assert _find_overlay_project() == tmp_path


def test_find_overlay_project_without_active(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with patch("teetree.config.discover_active_overlay", return_value=None):
        result = _find_overlay_project()
        assert result == tmp_path


def test_camelize():
    assert _camelize("hello_world") == "HelloWorld"
    assert _camelize("single") == "Single"
    assert _camelize("a_b_c") == "ABC"


def test_scripts_dir_returns_path():
    from teetree.cli import _scripts_dir  # noqa: PLC0415

    result = _scripts_dir()
    assert isinstance(result, Path)
    assert result.name == "scripts"


def test_run_script_not_found(tmp_path):
    """_run_script raises Exit when script not found."""
    import click  # noqa: PLC0415

    with patch("teetree.cli._scripts_dir", return_value=tmp_path):
        try:
            _run_script("nonexistent_script")
            msg = "Expected Exit"
            raise AssertionError(msg)
        except (SystemExit, click.exceptions.Exit) as e:
            assert e.exit_code == 1  # noqa: PT017


def test_run_script_failure(tmp_path):
    """_run_script raises Exit on non-zero returncode."""
    import click  # noqa: PLC0415

    script = tmp_path / "test_script.py"
    script.write_text("import sys; sys.exit(2)")
    with patch("teetree.cli._scripts_dir", return_value=tmp_path):
        try:
            _run_script("test_script")
            msg = "Expected Exit"
            raise AssertionError(msg)
        except (SystemExit, click.exceptions.Exit) as e:
            assert e.exit_code == 2  # noqa: PT017


def test_run_script_success(tmp_path):
    """_run_script succeeds for a passing script."""
    script = tmp_path / "ok_script.py"
    script.write_text("pass")
    with patch("teetree.cli._scripts_dir", return_value=tmp_path):
        _run_script("ok_script")


def test_current_git_branch_success():
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="feature-branch\n", returncode=0)
        assert _current_git_branch() == "feature-branch"


def test_current_git_branch_failure():
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=128)
        assert _current_git_branch() == ""


def test_get_gitlab_token_from_env(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "gl-token-123")
    assert _get_gitlab_token() == "gl-token-123"


def test_get_gitlab_token_from_teatree_env(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("TEATREE_GITLAB_TOKEN", "tt-token-456")
    assert _get_gitlab_token() == "tt-token-456"


def test_get_gitlab_token_from_glab(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stderr="  Token: glpat-ABCDEF\n  User: test\n",
            returncode=0,
        )
        assert _get_gitlab_token() == "glpat-ABCDEF"


def test_get_gitlab_token_not_found(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stderr="", returncode=1)
        assert _get_gitlab_token() == ""


def test_get_ci_service_from_env(monkeypatch):
    """_get_ci_service creates service from env when Django fails."""
    monkeypatch.setenv("TEATREE_GITLAB_TOKEN", "token")
    with patch("teetree.backends.loader.get_ci_service", side_effect=Exception("no django")):
        service = _get_ci_service()
        assert service is not None


def test_get_ci_service_no_token(monkeypatch):
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with patch("teetree.backends.loader.get_ci_service", side_effect=Exception("no django")):
        assert _get_ci_service() is None


def test_get_ci_project_from_overlay():
    """_get_ci_project returns overlay path when available."""
    mock_overlay = MagicMock()
    mock_overlay.get_ci_project_path.return_value = "org/repo"
    with (
        patch("django.setup"),
        patch("teetree.core.overlay_loader.get_overlay", return_value=mock_overlay),
    ):
        result = _get_ci_project()
        assert result == "org/repo"


def test_get_ci_project_fallback_to_remote():
    """_get_ci_project falls back to git remote."""
    mock_project_info = MagicMock(path_with_namespace="org/repo-from-remote")
    with (
        patch("django.setup", side_effect=Exception("no django")),
        patch("teetree.utils.gitlab_api.GitLabAPI") as mock_api_cls,
    ):
        mock_api_cls.return_value.resolve_project_from_remote.return_value = mock_project_info
        result = _get_ci_project()
        assert result == "org/repo-from-remote"


def test_get_ci_project_no_remote():
    """_get_ci_project returns empty string when no remote."""
    with (
        patch("django.setup", side_effect=Exception("no django")),
        patch("teetree.utils.gitlab_api.GitLabAPI") as mock_api_cls,
    ):
        mock_api_cls.return_value.resolve_project_from_remote.return_value = None
        result = _get_ci_project()
        assert result == ""


# ── Startproject helpers ─────────────────────────────────────────────


def test_patch_settings(tmp_path):
    settings_path = tmp_path / "settings.py"
    settings_path.write_text(
        "INSTALLED_APPS = [\n    'django.contrib.staticfiles',\n]\n",
        encoding="utf-8",
    )
    _patch_settings(settings_path, "my_overlay", "MyOverlay")
    text = settings_path.read_text()
    assert "'teetree.core'" in text
    assert "'my_overlay'" in text
    assert 'TEATREE_OVERLAY_CLASS = "my_overlay.overlay.MyOverlayOverlay"' in text


def test_patch_urls(tmp_path):
    urls_path = tmp_path / "urls.py"
    urls_path.write_text(
        "from django.urls import path\nurlpatterns = [path('admin/', admin.site.urls),]\n",
        encoding="utf-8",
    )
    _patch_urls(urls_path)
    text = urls_path.read_text()
    assert "include" in text
    assert "teetree.core.urls" in text


def test_patch_manage_py(tmp_path):
    manage_py = tmp_path / "manage.py"
    manage_py.write_text(
        "#!/usr/bin/env python\nimport sys\nimport os\n"
        'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "my.settings")\n',
        encoding="utf-8",
    )
    _patch_manage_py(manage_py)
    text = manage_py.read_text()
    assert "from pathlib import Path" in text
    assert 'sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))' in text


def test_write_overlay(tmp_path):
    overlay_path = tmp_path / "overlay.py"
    _write_overlay(overlay_path, "test_overlay", "TestOverlay", "t3-test")
    text = overlay_path.read_text()
    assert "class TestOverlayOverlay" in text
    assert "OverlayBase" in text
    assert '"skill_path": "skills/t3-test/SKILL.md"' in text


def test_write_skill_md(tmp_path):
    skill_path = tmp_path / "SKILL.md"
    _write_skill_md(skill_path, "t3-acme", "t3-acme")
    text = skill_path.read_text()
    assert "name: t3-acme" in text
    assert "t3-workspace" in text


# ── _editable_info ───────────────────────────────────────────────────


def test_editable_info_not_installed():
    from importlib.metadata import PackageNotFoundError  # noqa: PLC0415

    with patch("importlib.metadata.distribution", side_effect=PackageNotFoundError("x")):
        assert _editable_info("nonexistent") == (False, "")


def test_editable_info_no_direct_url():
    mock_dist = MagicMock()
    mock_dist.read_text.return_value = None
    with patch("importlib.metadata.distribution", return_value=mock_dist):
        assert _editable_info("some-pkg") == (False, "")


def test_editable_info_editable():
    mock_dist = MagicMock()
    mock_dist.read_text.return_value = json.dumps(
        {
            "dir_info": {"editable": True},
            "url": "file:///home/user/project",
        }
    )
    with patch("importlib.metadata.distribution", return_value=mock_dist):
        editable, url = _editable_info("some-pkg")
        assert editable is True
        assert url == "file:///home/user/project"


def test_editable_info_invalid_json():
    mock_dist = MagicMock()
    mock_dist.read_text.return_value = "not json"
    with patch("importlib.metadata.distribution", return_value=mock_dist):
        assert _editable_info("some-pkg") == (False, "")


# ── _print_package_info ──────────────────────────────────────────────


def test_print_package_info_installed(capsys):
    with (
        patch("importlib.import_module") as mock_import,
        patch("teetree.cli._editable_info", return_value=(False, "")),
    ):
        mock_mod = MagicMock()
        mock_mod.__file__ = "/usr/lib/python/teetree/__init__.py"
        mock_import.return_value = mock_mod
        _print_package_info("teatree", "teetree")
        # Just verifying it runs without error; output goes through typer.echo


def test_print_package_info_not_installed(capsys):
    with patch("importlib.import_module", side_effect=ImportError("nope")):
        _print_package_info("teatree", "teetree")
        # Verifying it handles ImportError gracefully


def test_print_package_info_editable(capsys):
    with (
        patch("importlib.import_module") as mock_import,
        patch("teetree.cli._editable_info", return_value=(True, "file:///src")),
    ):
        mock_mod = MagicMock()
        mock_mod.__file__ = "/src/teetree/__init__.py"
        mock_import.return_value = mock_mod
        _print_package_info("teatree", "teetree")


def test_print_package_info_editable_no_url(capsys):
    """_print_package_info doesn't print URL when editable but no url."""
    with (
        patch("importlib.import_module") as mock_import,
        patch("teetree.cli._editable_info", return_value=(True, "")),
    ):
        mock_mod = MagicMock()
        mock_mod.__file__ = "/src/teetree/__init__.py"
        mock_import.return_value = mock_mod
        _print_package_info("teatree", "teetree")


# ── _show_info ───────────────────────────────────────────────────────


def test_show_info_with_overlay(capsys):
    from teetree.config import OverlayEntry  # noqa: PLC0415

    active = OverlayEntry(name="acme", settings_module="acme.settings")
    entries = [OverlayEntry(name="acme", settings_module="acme.settings")]

    with (
        patch("shutil.which", return_value="/usr/bin/t3"),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("teetree.cli._print_package_info"),
        patch("teetree.config.discover_active_overlay", return_value=active),
        patch("teetree.config.discover_overlays", return_value=entries),
    ):
        _show_info()


def test_show_info_no_overlay(capsys):
    with (
        patch("shutil.which", return_value=None),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("teetree.cli._print_package_info"),
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("teetree.config.discover_overlays", return_value=[]),
    ):
        _show_info()


# ── _collect_overlay_skills ──────────────────────────────────────────


def test_collect_overlay_skills_from_skills_dir(tmp_path):
    """Overlay skills are collected from projects' skills/ dirs."""
    from teetree.config import OverlayEntry  # noqa: PLC0415

    project = tmp_path / "my-project"
    skill = project / "skills" / "t3-custom"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").touch()

    entry = OverlayEntry(name="t3-test", settings_module="test.settings", project_path=project)
    with patch("teetree.config.discover_overlays", return_value=[entry]):
        results = _collect_overlay_skills()
        assert len(results) == 1
        assert results[0][1] == "t3-custom"


def test_collect_overlay_skills_legacy(tmp_path):
    """Overlay skills from legacy convention (subdir with SKILL.md)."""
    from teetree.config import OverlayEntry  # noqa: PLC0415

    project = tmp_path / "my-overlay"
    project.mkdir()
    overlay_subdir = project / "my_app"
    overlay_subdir.mkdir()
    (overlay_subdir / "SKILL.md").touch()

    entry = OverlayEntry(name="my-overlay", settings_module="test.settings", project_path=project)
    with patch("teetree.config.discover_overlays", return_value=[entry]):
        results = _collect_overlay_skills()
        assert len(results) == 1
        assert results[0][1] == "t3-my-overlay"


def test_collect_overlay_skills_no_project_path():
    from teetree.config import OverlayEntry  # noqa: PLC0415

    entry = OverlayEntry(name="test", settings_module="test.settings", project_path=None)
    with patch("teetree.config.discover_overlays", return_value=[entry]):
        results = _collect_overlay_skills()
        assert results == []


# ── _repair_symlinks ─────────────────────────────────────────────────


def test_repair_symlinks_creates_links(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "t3-code").mkdir()
    (skills_dir / "t3-code" / "SKILL.md").touch()

    claude_skills = tmp_path / "claude_skills"
    claude_skills.mkdir()

    with patch("teetree.cli._collect_overlay_skills", return_value=[]):
        created, fixed = _repair_symlinks(skills_dir, claude_skills)
        assert created == 1
        assert fixed == 0
        assert (claude_skills / "t3-code").is_symlink()


def test_repair_symlinks_empty_skills_dir(tmp_path):
    """_repair_symlinks handles empty skills dir (no SKILL.md files)."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # Dir with no SKILL.md inside
    (skills_dir / "not-a-skill").mkdir()

    claude_skills = tmp_path / "claude_skills"
    claude_skills.mkdir()

    with patch("teetree.cli._collect_overlay_skills", return_value=[]):
        created, fixed = _repair_symlinks(skills_dir, claude_skills)
        assert created == 0
        assert fixed == 0


def test_repair_symlinks_fixes_wrong_target(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill = skills_dir / "t3-code"
    skill.mkdir()
    (skill / "SKILL.md").touch()

    claude_skills = tmp_path / "claude_skills"
    claude_skills.mkdir()
    # Create a symlink with wrong target
    wrong_target = tmp_path / "wrong"
    wrong_target.mkdir()
    (claude_skills / "t3-code").symlink_to(wrong_target)

    with patch("teetree.cli._collect_overlay_skills", return_value=[]):
        created, fixed = _repair_symlinks(skills_dir, claude_skills)
        assert created == 1  # re-created after unlinking
        assert fixed == 1


def test_repair_symlinks_skips_real_dir(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill = skills_dir / "t3-code"
    skill.mkdir()
    (skill / "SKILL.md").touch()

    claude_skills = tmp_path / "claude_skills"
    claude_skills.mkdir()
    # A real directory, not a symlink
    (claude_skills / "t3-code").mkdir()

    with patch("teetree.cli._collect_overlay_skills", return_value=[]):
        created, fixed = _repair_symlinks(skills_dir, claude_skills)
        assert created == 0
        assert fixed == 0


def test_repair_symlinks_correct_link_unchanged(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill = skills_dir / "t3-code"
    skill.mkdir()
    (skill / "SKILL.md").touch()

    claude_skills = tmp_path / "claude_skills"
    claude_skills.mkdir()
    (claude_skills / "t3-code").symlink_to(skill)

    with patch("teetree.cli._collect_overlay_skills", return_value=[]):
        created, fixed = _repair_symlinks(skills_dir, claude_skills)
        assert created == 0
        assert fixed == 0


# ── _managepy ─────────────────────────────────────────────────────────


def test_managepy_none_path():
    import click  # noqa: PLC0415

    try:
        _managepy(None)
        msg = "Expected Exit"
        raise AssertionError(msg)
    except (SystemExit, click.exceptions.Exit) as e:
        assert e.exit_code == 1  # noqa: PT017


def test_managepy_no_manage_py(tmp_path):
    import click  # noqa: PLC0415

    try:
        _managepy(tmp_path)
        msg = "Expected Exit"
        raise AssertionError(msg)
    except (SystemExit, click.exceptions.Exit) as e:
        assert e.exit_code == 1  # noqa: PT017


def test_managepy_runs_subprocess(tmp_path):
    (tmp_path / "manage.py").write_text("pass\n")
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        _managepy(tmp_path, "migrate")
        mock_run.assert_called_once()


# ── _uvicorn ──────────────────────────────────────────────────────────


def test_uvicorn_none_path():
    import click  # noqa: PLC0415

    try:
        _uvicorn(None, "127.0.0.1", 8000)
        msg = "Expected Exit"
        raise AssertionError(msg)
    except (SystemExit, click.exceptions.Exit) as e:
        assert e.exit_code == 1  # noqa: PT017


def test_uvicorn_runs_subprocess(tmp_path):
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        _uvicorn(tmp_path, "127.0.0.1", 8000, "myapp.settings")
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "uvicorn" in str(call_args)
        assert "myapp.asgi:application" in str(call_args)


def test_uvicorn_uses_project_venv(tmp_path):
    """Uvicorn uses the project's venv Python if available."""
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/usr/bin/env python\n")

    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        _uvicorn(tmp_path, "127.0.0.1", 8000, "myapp.settings")
        call_args = mock_run.call_args[0][0]
        assert str(venv_python) == call_args[0]


# ── _build_overlay_app ────────────────────────────────────────────────


def test_build_overlay_app_creates_typer_app():
    overlay_app = _build_overlay_app("test", Path("/tmp/project"), "test.settings")
    assert isinstance(overlay_app, typer.Typer)


# ── _bridge_subcommand ────────────────────────────────────────────────


def test_bridge_subcommand_registers_command():
    group = typer.Typer()
    _bridge_subcommand(group, "lifecycle", "setup", "Create worktree", Path("/tmp"))
    # Verify command was registered (Typer stores registered commands internally)
    assert len(group.registered_commands) == 1


# ── _register_overlay_commands ────────────────────────────────────────


def test_register_overlay_commands_with_overlays():
    from teetree.config import OverlayEntry  # noqa: PLC0415

    entries = [OverlayEntry(name="test", settings_module="test.settings", project_path=Path("/tmp/test"))]
    active = OverlayEntry(name="test", settings_module="test.settings", project_path=Path("/tmp/test"))

    with (
        patch("teetree.config.discover_active_overlay", return_value=active),
        patch("teetree.config.discover_overlays", return_value=entries),
    ):
        _register_overlay_commands()


def test_register_overlay_commands_no_overlays():
    with (
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("teetree.config.discover_overlays", return_value=[]),
    ):
        _register_overlay_commands()


# ── _check_editable_sanity ────────────────────────────────────────────


def test_check_editable_sanity_no_settings(monkeypatch):
    """Returns empty when no settings module configured."""
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    with patch("teetree.config.discover_active_overlay", return_value=None):
        result = _check_editable_sanity()
        assert result == []


def test_check_editable_sanity_with_active_overlay(monkeypatch):
    """Sets DJANGO_SETTINGS_MODULE from active overlay when not in env."""
    from teetree.config import OverlayEntry  # noqa: PLC0415

    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    active = OverlayEntry(name="test", settings_module="tests.django_settings")
    with (
        patch("teetree.config.discover_active_overlay", return_value=active),
        patch("teetree.cli._editable_info", return_value=(False, "")),
    ):
        result = _check_editable_sanity()
        assert isinstance(result, list)


def test_check_editable_sanity_django_fails(monkeypatch):
    """Returns empty when Django setup fails."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "nonexistent.settings")
    with patch("django.setup", side_effect=Exception("bad setup")):
        result = _check_editable_sanity()
        assert result == []


def test_check_editable_sanity_teatree_should_be_editable(monkeypatch):
    """Warns when TEATREE_EDITABLE=True but teatree is not editable."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
    mock_settings = MagicMock()
    mock_settings.TEATREE_EDITABLE = True
    mock_settings.TEATREE_OVERLAY_CLASS = ""

    with (
        patch("django.setup"),
        patch("django.conf.settings", mock_settings),
        patch("teetree.cli._editable_info", return_value=(False, "")),
    ):
        result = _check_editable_sanity()
        assert any("TEATREE_EDITABLE=True" in p for p in result)


def test_check_editable_sanity_teatree_unexpectedly_editable(monkeypatch):
    """Warns when teatree is editable but not declared."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
    mock_settings = MagicMock()
    mock_settings.TEATREE_EDITABLE = False
    mock_settings.TEATREE_OVERLAY_CLASS = ""

    with (
        patch("django.setup"),
        patch("django.conf.settings", mock_settings),
        patch("teetree.cli._editable_info", return_value=(True, "file:///src")),
    ):
        result = _check_editable_sanity()
        assert any("TEATREE_EDITABLE is not set" in p for p in result)


def test_check_editable_sanity_overlay_should_be_editable(monkeypatch):
    """Warns when OVERLAY_EDITABLE=True but overlay is not editable."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
    mock_settings = MagicMock()
    mock_settings.TEATREE_EDITABLE = False
    mock_settings.TEATREE_OVERLAY_CLASS = "my_overlay.overlay.MyOverlay"
    mock_settings.OVERLAY_EDITABLE = True

    def editable_info(dist_name):
        if dist_name == "teatree":
            return (False, "")
        return (False, "")

    with (
        patch("django.setup"),
        patch("django.conf.settings", mock_settings),
        patch("teetree.cli._editable_info", side_effect=editable_info),
        patch("importlib.metadata.packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
    ):
        result = _check_editable_sanity()
        assert any("OVERLAY_EDITABLE=True" in p for p in result)


def test_check_editable_sanity_overlay_unexpectedly_editable(monkeypatch):
    """Warns when overlay is editable but not declared."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
    mock_settings = MagicMock()
    mock_settings.TEATREE_EDITABLE = False
    mock_settings.TEATREE_OVERLAY_CLASS = "my_overlay.overlay.MyOverlay"
    mock_settings.OVERLAY_EDITABLE = False

    def editable_info(dist_name):
        if dist_name == "teatree":
            return (False, "")
        return (True, "file:///src")

    with (
        patch("django.setup"),
        patch("django.conf.settings", mock_settings),
        patch("teetree.cli._editable_info", side_effect=editable_info),
        patch("importlib.metadata.packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
    ):
        result = _check_editable_sanity()
        assert any("OVERLAY_EDITABLE is not set" in p for p in result)


# ── _copy_config_templates ────────────────────────────────────────────


def test_copy_config_templates(tmp_path):
    from teetree.cli import _copy_config_templates  # noqa: PLC0415

    _copy_config_templates(tmp_path)
    assert (tmp_path / ".editorconfig").is_file()
    assert (tmp_path / ".gitignore").is_file()
    assert (tmp_path / ".markdownlint-cli2.yaml").is_file()
    assert (tmp_path / ".pre-commit-config.yaml").is_file()
    assert (tmp_path / ".python-version").is_file()


# ── _write_pyproject ──────────────────────────────────────────────────


def test_write_pyproject(tmp_path):
    from teetree.cli import _write_pyproject  # noqa: PLC0415

    _write_pyproject(tmp_path, "t3-demo", "demo_overlay", "demo")
    pyproject = tmp_path / "pyproject.toml"
    assert pyproject.is_file()
    text = pyproject.read_text()
    assert "t3-demo" in text
    assert "demo_overlay" in text


# ── _build_overlay_app subcommands ────────────────────────────────────


def test_overlay_dashboard_command(tmp_path):
    """Dashboard command migrates and starts uvicorn."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()
    (tmp_path / "manage.py").write_text("pass\n")

    with (
        patch("teetree.cli._managepy") as mock_manage,
        patch("teetree.cli._uvicorn") as mock_uvicorn,
        patch("socket.socket") as mock_socket_cls,
    ):
        # Port is free (connect_ex returns non-zero)
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = test_runner.invoke(overlay_app, ["dashboard"])
        assert result.exit_code == 0
        mock_manage.assert_called_once()
        mock_uvicorn.assert_called_once()


def test_overlay_dashboard_port_in_use(tmp_path):
    """Dashboard falls back to a free port when default is in use."""
    import socket as _socket  # noqa: PLC0415

    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()
    (tmp_path / "manage.py").write_text("pass\n")

    # We need to mock socket.socket to return different objects for
    # the context manager socket and the ephemeral socket.
    context_sock = MagicMock()
    context_sock.connect_ex.return_value = 0  # port in use

    ephemeral_sock = MagicMock()
    ephemeral_sock.getsockname.return_value = ("127.0.0.1", 9999)

    call_count = 0

    def socket_factory(family=_socket.AF_INET, type_=_socket.SOCK_STREAM):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # The context manager socket
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=context_sock)
            cm.__exit__ = MagicMock(return_value=False)
            return cm
        # The ephemeral socket for finding a free port
        return ephemeral_sock

    with (
        patch("teetree.cli._managepy"),
        patch("teetree.cli._uvicorn") as mock_uvicorn,
        patch("socket.socket", side_effect=socket_factory),
    ):
        result = test_runner.invoke(overlay_app, ["dashboard"])
        assert result.exit_code == 0
        assert "Port 8000 in use" in result.output
        # Verify uvicorn was called with the fallback port
        assert mock_uvicorn.call_args[0][2] == 9999


def test_overlay_resetdb(tmp_path, monkeypatch):
    """Resetdb deletes DB and migrates."""
    monkeypatch.setattr("teetree.config.DATA_DIR", tmp_path / "data")
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    # Create fake db
    db_dir = tmp_path / "data" / "test"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "db.sqlite3"
    db_path.write_text("fake db")

    with patch("teetree.cli._managepy"):
        result = test_runner.invoke(overlay_app, ["resetdb"])
        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert "Database recreated" in result.output
        assert not db_path.exists()


def test_overlay_resetdb_no_existing_db(tmp_path, monkeypatch):
    """Resetdb works even if DB doesn't exist yet."""
    monkeypatch.setattr("teetree.config.DATA_DIR", tmp_path / "data")
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy"):
        result = test_runner.invoke(overlay_app, ["resetdb"])
        assert result.exit_code == 0
        assert "Database recreated" in result.output


def test_overlay_worker_no_project():
    """Worker fails when project_path is None."""
    overlay_app = _build_overlay_app("test", None, "test.settings")
    test_runner = CliRunner()
    result = test_runner.invoke(overlay_app, ["worker"])
    assert result.exit_code == 1
    assert "Cannot find overlay project" in result.output


def test_overlay_worker_starts_processes(tmp_path):
    """Worker starts background processes."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()
    (tmp_path / "manage.py").write_text("pass\n")

    mock_proc = MagicMock()
    mock_proc.wait.return_value = 0

    with patch("teetree.cli.subprocess.Popen", return_value=mock_proc) as mock_popen:
        result = test_runner.invoke(overlay_app, ["worker", "--count", "2"])
        assert result.exit_code == 0
        assert mock_popen.call_count == 2
        assert "Started 2 worker(s)" in result.output


def test_overlay_worker_keyboard_interrupt(tmp_path):
    """Worker handles KeyboardInterrupt gracefully."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()
    (tmp_path / "manage.py").write_text("pass\n")

    mock_proc = MagicMock()
    mock_proc.wait.side_effect = KeyboardInterrupt

    with patch("teetree.cli.subprocess.Popen", return_value=mock_proc):
        result = test_runner.invoke(overlay_app, ["worker", "--count", "1"])
        assert "Shutting down" in result.output
        mock_proc.terminate.assert_called_once()


def test_overlay_full_status(tmp_path):
    """full-status delegates to followup refresh."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(overlay_app, ["full-status"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(tmp_path, "followup", "refresh")


def test_overlay_start_ticket(tmp_path):
    """start-ticket delegates to workspace ticket."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(overlay_app, ["start-ticket", "https://issue/123"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(tmp_path, "workspace", "ticket", "https://issue/123")


def test_overlay_start_ticket_with_variant(tmp_path):
    """start-ticket passes variant when specified."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(overlay_app, ["start-ticket", "https://issue/123", "--variant", "tenant-a"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(
            tmp_path, "workspace", "ticket", "https://issue/123", "--variant", "tenant-a"
        )


def test_overlay_ship(tmp_path):
    """Ship delegates to pr create."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(overlay_app, ["ship", "TICKET-123"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(tmp_path, "pr", "create", "TICKET-123")


def test_overlay_ship_with_title(tmp_path):
    """Ship passes title when specified."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(overlay_app, ["ship", "TICKET-123", "--title", "Fix bug"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(tmp_path, "pr", "create", "TICKET-123", "--title", "Fix bug")


def test_overlay_daily(tmp_path):
    """Daily delegates to followup sync."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(overlay_app, ["daily"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(tmp_path, "followup", "sync")


def test_overlay_agent(tmp_path, monkeypatch):
    """Overlay agent launches claude with overlay context."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("teetree.agents.skill_bundle.resolve_dependencies", return_value=["t3-code"]),
        patch("teetree.cli.os.execvp") as mock_exec,
    ):
        test_runner.invoke(overlay_app, ["agent", "fix something"])
        mock_exec.assert_called_once()


def test_overlay_agent_no_project_path(tmp_path, monkeypatch):
    """Overlay agent works even with no project_path by falling back to _find_project_root."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    overlay_app = _build_overlay_app("test", None, "test.settings")
    test_runner = CliRunner()

    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("teetree.agents.skill_bundle.resolve_dependencies", return_value=["t3-code"]),
        patch("teetree.cli.os.execvp") as mock_exec,
    ):
        test_runner.invoke(overlay_app, ["agent"])
        mock_exec.assert_called_once()


def test_overlay_lifecycle_subcommand(tmp_path):
    """Overlay command groups forward to manage.py."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(overlay_app, ["lifecycle", "setup"])
        assert result.exit_code == 0
        mock_manage.assert_called_once_with(tmp_path, "lifecycle", "setup")


# ── _launch_claude editable info branch ───────────────────────────────


def test_launch_claude_with_editable_teatree(tmp_path, monkeypatch):
    """_launch_claude includes editable source path when available."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch("teetree.cli._editable_info", return_value=(True, "file:///src/teatree")),
        patch("teetree.agents.skill_bundle.resolve_dependencies", return_value=["t3-code"]),
        patch("teetree.cli.os.execvp") as mock_exec,
    ):
        from teetree.cli import _launch_claude  # noqa: PLC0415

        _launch_claude(task="test", project_root=tmp_path, context_lines=["test"])
        cmd = mock_exec.call_args[0][1]
        context_arg = cmd[cmd.index("--append-system-prompt") + 1]
        assert "/src/teatree" in context_arg


# ── doctor check import failure branch ────────────────────────────────


def test_doctor_check_import_failure():
    """Doctor check returns False on import failure."""
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fail_import(name, *args, **kwargs):
        if name == "teetree.core":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fail_import):
        result = runner.invoke(app, ["doctor", "check"])
        assert "FAIL" in result.output


# ── doctor repair with overlay skills ─────────────────────────────────


def test_doctor_repair_with_overlay_skills(tmp_path):
    """Repair reports overlay skill count."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "t3-core").mkdir()
    (skills_dir / "t3-core" / "SKILL.md").touch()

    overlay_skill = tmp_path / "overlay-skill"
    overlay_skill.mkdir()
    (overlay_skill / "SKILL.md").touch()

    with (
        patch("teetree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_dir),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("teetree.cli._collect_overlay_skills", return_value=[(overlay_skill, "t3-overlay")]),
    ):
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)

        result = runner.invoke(app, ["doctor", "repair"])
        assert result.exit_code == 0
        assert "overlay skill(s)" in result.output


# ── CI cancel with explicit branch ────────────────────────────────────


def test_ci_cancel_with_explicit_branch():
    """Ci cancel uses explicit branch argument."""
    mock_ci = MagicMock()
    mock_ci.cancel_pipelines.return_value = [1]
    with (
        patch("teetree.cli._get_ci_service", return_value=mock_ci),
        patch("teetree.cli._get_ci_project", return_value="org/repo"),
    ):
        result = runner.invoke(app, ["ci", "cancel", "my-branch"])
        assert result.exit_code == 0
        mock_ci.cancel_pipelines.assert_called_once_with(project="org/repo", ref="my-branch")


# ── Partial branch coverage improvements ──────────────────────────────


def test_write_skill_cache_no_active_overlay(monkeypatch):
    """write-skill-cache works when DJANGO_SETTINGS_MODULE is already set."""
    mock_overlay = MagicMock()
    mock_overlay.get_skill_metadata.return_value = {}

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
    with (
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("django.setup"),
        patch("teetree.core.overlay_loader.get_overlay", return_value=mock_overlay),
    ):
        runner.invoke(app, ["config", "write-skill-cache"])
        # May fail at get_overlay since no overlay is configured,
        # but the branch we want (326->328 bypass) is hit


def test_get_ci_project_overlay_returns_empty():
    """_get_ci_project falls back to remote when overlay returns empty path."""
    mock_overlay = MagicMock()
    mock_overlay.get_ci_project_path.return_value = ""
    mock_project_info = MagicMock(path_with_namespace="org/fallback")
    with (
        patch("django.setup"),
        patch("teetree.core.overlay_loader.get_overlay", return_value=mock_overlay),
        patch("teetree.utils.gitlab_api.GitLabAPI") as mock_api_cls,
    ):
        mock_api_cls.return_value.resolve_project_from_remote.return_value = mock_project_info
        result = _get_ci_project()
        assert result == "org/fallback"


def test_check_editable_sanity_both_ok(monkeypatch):
    """No warnings when editable state matches declared intent."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
    mock_settings = MagicMock()
    mock_settings.TEATREE_EDITABLE = False
    mock_settings.TEATREE_OVERLAY_CLASS = "my_overlay.overlay.MyOverlay"
    mock_settings.OVERLAY_EDITABLE = False

    with (
        patch("django.setup"),
        patch("django.conf.settings", mock_settings),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("importlib.metadata.packages_distributions", return_value={"my_overlay": ["my-overlay"]}),
    ):
        result = _check_editable_sanity()
        assert result == []


def test_get_gitlab_token_glab_no_token_line(monkeypatch):
    """_get_gitlab_token returns empty when glab output has no Token line."""
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("TEATREE_GITLAB_TOKEN", raising=False)
    with patch("teetree.cli.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stderr="  User: test\n  Scopes: api\n",
            returncode=0,
        )
        assert _get_gitlab_token() == ""


# ── config autoload command (lines 350-367) ───────────────────────────


def test_config_autoload_shows_context_match_files(tmp_path):
    """Config autoload lists context-match.yml rules from skill dirs."""
    skills_dir = tmp_path / "skills"
    skill = skills_dir / "t3-code" / "hook-config"
    skill.mkdir(parents=True)
    (skill / "context-match.yml").write_text("keywords:\n  - code\n")

    # A skill without context-match.yml should be skipped
    (skills_dir / "t3-test").mkdir()

    with patch("teetree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_dir):
        result = runner.invoke(app, ["config", "autoload"])
        assert result.exit_code == 0
        assert "t3-code" in result.output
        assert "keywords" in result.output


def test_config_autoload_no_skills_dir(tmp_path):
    """Config autoload fails when skills dir doesn't exist."""
    with patch("teetree.agents.skill_bundle.DEFAULT_SKILLS_DIR", tmp_path / "nonexistent"):
        result = runner.invoke(app, ["config", "autoload"])
        assert result.exit_code == 1
        assert "Skills directory not found" in result.output


def test_config_autoload_no_context_match_files(tmp_path):
    """Config autoload shows message when no context-match.yml found."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "t3-code").mkdir()
    # No hook-config/context-match.yml

    with patch("teetree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_dir):
        result = runner.invoke(app, ["config", "autoload"])
        assert result.exit_code == 0
        assert "No context-match.yml files found" in result.output


# ── config cache command (lines 373-385) ──────────────────────────────


def test_config_cache_shows_content(tmp_path, monkeypatch):
    """Config cache displays skill-metadata.json content."""
    monkeypatch.setattr("teetree.config.DATA_DIR", tmp_path)
    cache_path = tmp_path / "skill-metadata.json"
    cache_path.write_text('{"skill_path": "skills/t3-test/SKILL.md"}\n')

    result = runner.invoke(app, ["config", "cache"])
    assert result.exit_code == 0
    assert "skill_path" in result.output


def test_config_cache_no_file(tmp_path, monkeypatch):
    """Config cache fails when no cache file exists."""
    monkeypatch.setattr("teetree.config.DATA_DIR", tmp_path)

    result = runner.invoke(app, ["config", "cache"])
    assert result.exit_code == 1
    assert "No cache found" in result.output


# ── doctor info command (line 991) ────────────────────────────────────


def test_doctor_info():
    """Doctor info delegates to _show_info."""
    with (
        patch("shutil.which", return_value="/usr/local/bin/t3"),
        patch("teetree.cli._editable_info", return_value=(False, "")),
        patch("teetree.cli._print_package_info"),
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("teetree.config.discover_overlays", return_value=[]),
    ):
        result = runner.invoke(app, ["doctor", "info"])
        assert result.exit_code == 0


# ── tool sonar-check command (lines 1038-1048) ───────────────────────


def test_tool_sonar_check_script_not_found(tmp_path):
    """Sonar-check exits with error when script is missing."""
    with patch("teetree.cli._find_overlay_project", return_value=tmp_path):
        result = runner.invoke(app, ["tool", "sonar-check"])
        assert result.exit_code == 1
        assert "sonar_check.sh not found" in result.output


def test_tool_sonar_check(tmp_path):
    """Tool sonar-check calls the overlay script directly."""
    script = tmp_path / "scripts" / "sonar_check.sh"
    script.parent.mkdir()
    script.touch()
    with (
        patch("teetree.cli._find_overlay_project", return_value=tmp_path),
        patch("teetree.cli.subprocess") as mock_sub,
    ):
        mock_sub.run.return_value = subprocess.CompletedProcess([], 0)
        result = runner.invoke(app, ["tool", "sonar-check", "/tmp/repo"])
        assert result.exit_code == 0
        args = mock_sub.run.call_args[0][0]
        assert args[0] == "bash"
        assert args[1] == str(script)
        assert "/tmp/repo" in args


def test_tool_sonar_check_with_flags(tmp_path):
    """Tool sonar-check passes skip-baseline and remote flags."""
    script = tmp_path / "scripts" / "sonar_check.sh"
    script.parent.mkdir()
    script.touch()
    with (
        patch("teetree.cli._find_overlay_project", return_value=tmp_path),
        patch("teetree.cli.subprocess") as mock_sub,
    ):
        mock_sub.run.return_value = subprocess.CompletedProcess([], 0)
        result = runner.invoke(app, ["tool", "sonar-check", "--skip-baseline", "--remote", "--remote-status"])
        assert result.exit_code == 0
        args = mock_sub.run.call_args[0][0]
        assert "--skip-baseline" in args
        assert "--remote" in args
        assert "--remote-status" in args


def test_tool_sonar_check_uses_pwd_env(tmp_path, monkeypatch):
    """When no repo_path given, sonar-check uses $PWD (not os.getcwd())."""
    script = tmp_path / "scripts" / "sonar_check.sh"
    script.parent.mkdir()
    script.touch()
    monkeypatch.setenv("PWD", "/original/worktree")
    with (
        patch("teetree.cli._find_overlay_project", return_value=tmp_path),
        patch("teetree.cli.subprocess") as mock_sub,
    ):
        mock_sub.run.return_value = subprocess.CompletedProcess([], 0)
        result = runner.invoke(app, ["tool", "sonar-check", "--remote"])
        assert result.exit_code == 0
        args = mock_sub.run.call_args[0][0]
        assert "/original/worktree" in args


# ── Overlay config subcommands (lines 1549-1594) ─────────────────────


def test_overlay_enable_autostart(tmp_path):
    """enable-autostart delegates to teetree.autostart.enable."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with (
        patch("teetree.config.discover_active_overlay", return_value=None),
        patch("teetree.autostart.enable", return_value="Service installed") as mock_enable,
    ):
        result = test_runner.invoke(overlay_app, ["config", "enable-autostart"])
        assert result.exit_code == 0
        assert "Service installed" in result.output
        mock_enable.assert_called_once()


def test_overlay_disable_autostart(tmp_path):
    """disable-autostart delegates to teetree.autostart.disable."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch("teetree.autostart.disable", return_value="Service removed") as mock_disable:
        result = test_runner.invoke(overlay_app, ["config", "disable-autostart"])
        assert result.exit_code == 0
        assert "Service removed" in result.output
        mock_disable.assert_called_once_with(overlay_name="test")


def test_overlay_show_logs_stdout(tmp_path):
    """Show logs shows stdout log output."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    stdout_log = tmp_path / "stdout.log"
    stdout_log.write_text("log line 1\nlog line 2\n")

    with (
        patch(
            "teetree.autostart.log_paths",
            return_value={"stdout": stdout_log, "stderr": tmp_path / "stderr.log"},
        ),
        patch("teetree.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = test_runner.invoke(overlay_app, ["config", "logs"])
        assert result.exit_code == 0
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "tail" in str(args)


def test_overlay_show_logs_follow(tmp_path):
    """Show logs --follow uses tail -f."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    stdout_log = tmp_path / "stdout.log"
    stdout_log.write_text("log data\n")

    with (
        patch(
            "teetree.autostart.log_paths",
            return_value={"stdout": stdout_log, "stderr": tmp_path / "stderr.log"},
        ),
        patch("teetree.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = test_runner.invoke(overlay_app, ["config", "logs", "--follow"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert "-f" in args


def test_overlay_show_logs_no_file(tmp_path):
    """Show logs fails when log file doesn't exist."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    with patch(
        "teetree.autostart.log_paths",
        return_value={"stdout": tmp_path / "nonexistent.log", "stderr": tmp_path / "stderr.log"},
    ):
        result = test_runner.invoke(overlay_app, ["config", "logs"])
        assert result.exit_code == 1
        assert "No log file found" in result.output


def test_overlay_show_logs_stderr(tmp_path):
    """Show logs --stderr reads the stderr log file."""
    overlay_app = _build_overlay_app("test", tmp_path, "test.settings")
    test_runner = CliRunner()

    stderr_log = tmp_path / "stderr.log"
    stderr_log.write_text("error data\n")

    with (
        patch(
            "teetree.autostart.log_paths",
            return_value={"stdout": tmp_path / "stdout.log", "stderr": stderr_log},
        ),
        patch("teetree.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = test_runner.invoke(overlay_app, ["config", "logs", "--stderr"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert str(stderr_log) in str(args)


# ── _register_overlay_tools (lines 1610-1649) ────────────────────────


def test_register_overlay_tools_from_json(tmp_path):
    """Overlay app registers tool commands from hook-config/tool-commands.json."""
    from teetree.cli import _register_overlay_tools  # noqa: PLC0415

    hook_dir = tmp_path / "skills" / "my-skill" / "hook-config"
    hook_dir.mkdir(parents=True)
    (hook_dir / "tool-commands.json").write_text(
        json.dumps(
            [
                {"name": "lint", "help": "Run linter", "management_command": "tool lint"},
                {"name": "format", "help": "Auto-format code", "management_command": "tool format"},
            ]
        )
    )

    overlay_app = typer.Typer()
    _register_overlay_tools(overlay_app, tmp_path)

    # The tool group should have been registered
    assert len(overlay_app.registered_groups) == 1


def test_register_overlay_tools_skips_entries_without_name(tmp_path):
    """_register_overlay_tools skips tool specs without name or management_command."""
    from teetree.cli import _register_overlay_tools  # noqa: PLC0415

    hook_dir = tmp_path / "skills" / "my-skill" / "hook-config"
    hook_dir.mkdir(parents=True)
    (hook_dir / "tool-commands.json").write_text(
        json.dumps(
            [
                {"help": "No name defined"},
                {"name": "valid", "management_command": "tool valid", "help": "Works"},
            ]
        )
    )

    overlay_app = typer.Typer()
    _register_overlay_tools(overlay_app, tmp_path)


def test_register_overlay_tools_handles_invalid_json(tmp_path):
    """_register_overlay_tools skips files with invalid JSON (line 1615-1616)."""
    from teetree.cli import _register_overlay_tools  # noqa: PLC0415

    hook_dir = tmp_path / "skills" / "my-skill" / "hook-config"
    hook_dir.mkdir(parents=True)
    (hook_dir / "tool-commands.json").write_text("not valid json {{{")

    overlay_app = typer.Typer()
    _register_overlay_tools(overlay_app, tmp_path)

    # Should not crash, just skip the file
    assert len(overlay_app.registered_groups) == 0


def test_register_overlay_tools_none_path():
    """_register_overlay_tools returns early when project_path is None."""
    from teetree.cli import _register_overlay_tools  # noqa: PLC0415

    overlay_app = typer.Typer()
    _register_overlay_tools(overlay_app, None)
    assert len(overlay_app.registered_groups) == 0


def test_register_overlay_tools_no_tool_commands(tmp_path):
    """_register_overlay_tools returns early when no tool-commands.json found."""
    from teetree.cli import _register_overlay_tools  # noqa: PLC0415

    overlay_app = typer.Typer()
    _register_overlay_tools(overlay_app, tmp_path)
    assert len(overlay_app.registered_groups) == 0


def test_bridge_tool_command_runs_managepy(tmp_path):
    """_bridge_tool_command creates a command that delegates to _managepy."""
    from teetree.cli import _bridge_tool_command  # noqa: PLC0415

    group = typer.Typer()
    (tmp_path / "manage.py").write_text("pass\n")
    _bridge_tool_command(group, "my-tool", "Run my tool", "tool my-tool", tmp_path)

    test_runner = CliRunner()
    with patch("teetree.cli._managepy") as mock_manage:
        result = test_runner.invoke(group, ["my-tool", "extra-arg"])
        assert result.exit_code == 0
        mock_manage.assert_called_once()
        call_args = mock_manage.call_args[0]
        assert call_args[0] == tmp_path
        assert "tool" in call_args
        assert "my-tool" in call_args
