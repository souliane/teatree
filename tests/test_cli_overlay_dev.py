import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import teatree.cli.overlay_dev
from teatree.cli.overlay_dev import (
    OverlayDevError,
    _ensure_sibling_worktree,
    _resolve_overlay_source,
    _resolve_teatree_worktree,
    _uv_pip_install_editable,
    overlay_dev_app,
)


def _make_worktree(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
    (path / ".git").write_text("gitdir: /fake\n")
    return path


class TestOverlayDevModule:
    def test_module_importable(self) -> None:
        assert teatree.cli.overlay_dev is not None

    def test_has_typer_app(self) -> None:
        assert overlay_dev_app is not None


class TestResolveTeatreeWorktree:
    def test_returns_worktree_root_when_cwd_is_worktree(self, tmp_path: Path) -> None:
        worktree = _make_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")

        assert _resolve_teatree_worktree(worktree) == worktree

    def test_walks_up_from_subdirectory(self, tmp_path: Path) -> None:
        worktree = _make_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        (worktree / "src" / "teatree").mkdir(parents=True)

        assert _resolve_teatree_worktree(worktree / "src" / "teatree") == worktree

    def test_refuses_main_clone(self, tmp_path: Path) -> None:
        clone = tmp_path / "souliane" / "teatree"
        clone.mkdir(parents=True)
        (clone / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        (clone / ".git").mkdir()

        with pytest.raises(OverlayDevError, match="main clone"):
            _resolve_teatree_worktree(clone)

    def test_refuses_non_teatree_dir(self, tmp_path: Path) -> None:
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "pyproject.toml").write_text('[project]\nname = "other"\n')
        (other / ".git").write_text("gitdir: /fake\n")

        with pytest.raises(OverlayDevError, match="not a teatree"):
            _resolve_teatree_worktree(other)

    def test_raises_when_no_pyproject_found(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        with pytest.raises(OverlayDevError, match="No teatree worktree"):
            _resolve_teatree_worktree(empty)


class TestResolveOverlaySource:
    def test_resolves_from_toml_path(self, tmp_path: Path) -> None:
        main_clone = tmp_path / "acme-workspace" / "example-overlay"
        main_clone.mkdir(parents=True)
        config = tmp_path / "teatree.toml"
        config.write_text(f'[overlays.example-overlay]\npath = "{main_clone}"\n')

        assert _resolve_overlay_source("example-overlay", config_path=config) == main_clone

    def test_raises_when_overlay_missing(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text("")

        with pytest.raises(OverlayDevError, match="not configured"):
            _resolve_overlay_source("ghost", config_path=config)

    def test_raises_when_path_missing(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.example-overlay]\nclass = "foo:Bar"\n')

        with pytest.raises(OverlayDevError, match="no path configured"):
            _resolve_overlay_source("example-overlay", config_path=config)


class TestEnsureSiblingWorktree:
    def test_returns_existing_sibling(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        teatree_wt = ticket_dir / "teatree"
        teatree_wt.mkdir(parents=True)
        sibling = ticket_dir / "example-overlay"
        sibling.mkdir()
        main_clone = tmp_path / "main" / "example-overlay"
        main_clone.mkdir(parents=True)

        assert _ensure_sibling_worktree(teatree_wt, main_clone, branch="any") == sibling

    def test_creates_sibling_via_git_worktree_add(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        teatree_wt = ticket_dir / "teatree"
        teatree_wt.mkdir(parents=True)
        main_clone = tmp_path / "main" / "example-overlay"
        main_clone.mkdir(parents=True)

        with patch("teatree.cli.overlay_dev.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = _ensure_sibling_worktree(teatree_wt, main_clone, branch="ac-teatree-120")

        assert result == ticket_dir / "example-overlay"
        cmds = [call.args[0] for call in run.call_args_list]
        assert any(("worktree" in c and "add" in c) for c in cmds)
        add_cmd = next(c for c in cmds if "add" in c)
        assert str(ticket_dir / "example-overlay") in add_cmd

    def test_falls_back_to_default_branch_when_branch_missing(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        teatree_wt = ticket_dir / "teatree"
        teatree_wt.mkdir(parents=True)
        main_clone = tmp_path / "main" / "example-overlay"
        main_clone.mkdir(parents=True)

        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            if "rev-parse" in cmd and "--verify" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="not a branch")
            if "symbolic-ref" in cmd:
                return MagicMock(returncode=0, stdout="refs/remotes/origin/development\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("teatree.cli.overlay_dev.subprocess.run", side_effect=fake_run):
            _ensure_sibling_worktree(teatree_wt, main_clone, branch="missing-branch")

        add_cmd = next(c for c in calls if "add" in c)
        assert "missing-branch" not in add_cmd
        assert "development" in add_cmd


class TestUvPipInstall:
    def test_runs_uv_pip_install_editable_no_deps(self, tmp_path: Path) -> None:
        worktree = tmp_path / "teatree"
        worktree.mkdir()
        overlay = tmp_path / "example-overlay"
        overlay.mkdir()

        with patch("teatree.cli.overlay_dev.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            _uv_pip_install_editable(worktree, overlay)

        cmd = run.call_args.args[0]
        assert cmd[:3] == ["uv", "pip", "install"]
        assert "--editable" in cmd
        assert "--no-deps" in cmd
        assert str(overlay) in cmd
        assert run.call_args.kwargs["cwd"] == worktree


class TestInstallCommand:
    def test_install_end_to_end(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        teatree_wt = _make_worktree(ticket_dir / "teatree")
        main_clone = tmp_path / "workspace" / "example-overlay"
        main_clone.mkdir(parents=True)
        config = tmp_path / "teatree.toml"
        config.write_text(f'[overlays.example-overlay]\npath = "{main_clone}"\n')
        monkeypatch.setattr("teatree.cli.overlay_dev.CONFIG_PATH", config)
        monkeypatch.chdir(teatree_wt)

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="main\n", stderr="")

        with patch("teatree.cli.overlay_dev.subprocess.run", side_effect=fake_run):
            result = CliRunner().invoke(overlay_dev_app, ["install", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert any(("worktree" in c and "add" in c) for c in captured)
        assert any(("uv" in c and "install" in c) for c in captured)
        state = json.loads((teatree_wt / ".t3.local.json").read_text())
        assert "example-overlay" in state["overlays"]


class TestUninstallCommand:
    def test_uninstall_removes_editable_and_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        teatree_wt = _make_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        (teatree_wt / ".t3.local.json").write_text('{"overlays": {"example-overlay": {"source": "/tmp/x"}}}')
        monkeypatch.chdir(teatree_wt)

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            captured.append(cmd)
            return MagicMock(returncode=0)

        with patch("teatree.cli.overlay_dev.subprocess.run", side_effect=fake_run):
            result = CliRunner().invoke(overlay_dev_app, ["uninstall", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert any(("uv" in c and "uninstall" in c and "example-overlay" in c) for c in captured)
        state = json.loads((teatree_wt / ".t3.local.json").read_text())
        assert "example-overlay" not in state["overlays"]


class TestStatusCommand:
    def test_status_lists_installed_overlays(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        teatree_wt = _make_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        (teatree_wt / ".t3.local.json").write_text(
            '{"overlays": {"example-overlay": {"source": "/tmp/example-overlay"}}}'
        )
        monkeypatch.chdir(teatree_wt)

        result = CliRunner().invoke(overlay_dev_app, ["status"])

        assert result.exit_code == 0, result.output
        assert "example-overlay" in result.output
        assert "/tmp/example-overlay" in result.output

    def test_status_reports_none_when_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        teatree_wt = _make_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        monkeypatch.chdir(teatree_wt)

        result = CliRunner().invoke(overlay_dev_app, ["status"])

        assert result.exit_code == 0
        assert "No overlays installed" in result.output
