"""Dev-mode overlay install across BOTH layouts teatree is developed in.

Standalone (upstream): teatree is its own repo, the workspace is a git worktree of
it, and an overlay lives in a separate checkout that gets a sibling worktree.

Vendored fork: core sits at ``<fork>/vendor/teatree`` and the git boundary is the
fork root, which also declares the overlay in its own entry points — so there is
no sibling to make and the main clone is the sanctioned edit target.
"""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import teatree.cli.overlay_dev
from teatree.cli.overlay_dev import (
    OverlayDevError,
    TeatreeWorkspace,
    _ensure_sibling_worktree,
    _overlay_resolves_inside,
    _resolve_overlay_source,
    _resolve_teatree_workspace,
    _uv_pip_install_editable,
    _workspace_declares_overlay,
    overlay_dev_app,
)

FORK_PYPROJECT = """\
[project]
name = "acme-fork"

[project.entry-points."teatree.overlays"]
example-overlay = "acme_overlay.overlay:AcmeOverlay"
"""


def _make_standalone_worktree(path: Path) -> Path:
    """Upstream layout: teatree's own pyproject at the root, ``.git`` a worktree file."""
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
    (path / ".git").write_text("gitdir: /fake\n")
    return path


def _make_vendored_fork(path: Path, *, git_marker: str = "dir", pyproject: str = FORK_PYPROJECT) -> Path:
    """Fork layout: core vendored under ``vendor/teatree``, git boundary at the root."""
    core = path / "vendor" / "teatree"
    core.mkdir(parents=True)
    (core / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
    (path / "pyproject.toml").write_text(pyproject)
    if git_marker == "dir":
        (path / ".git").mkdir()
    elif git_marker == "file":
        (path / ".git").write_text("gitdir: /fake\n")
    return path


def _entry_point(name: str = "example-overlay", value: str = "acme_overlay.overlay:AcmeOverlay") -> SimpleNamespace:
    return SimpleNamespace(name=name, value=value)


def _seed_cold_registry(db: Path, overlays: dict[str, dict]) -> None:
    """Seed a cold-readable config DB — the tier ``load_config`` reads the registry from."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps(overlays),),
        )
        conn.commit()
    finally:
        conn.close()


class TestOverlayDevModule:
    def test_module_importable(self) -> None:
        assert teatree.cli.overlay_dev is not None

    def test_has_typer_app(self) -> None:
        assert overlay_dev_app is not None


class TestResolveStandaloneWorkspace:
    def test_returns_worktree_root_when_cwd_is_worktree(self, tmp_path: Path) -> None:
        worktree = _make_standalone_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")

        workspace = _resolve_teatree_workspace(worktree)

        assert workspace == TeatreeWorkspace(root=worktree, core=worktree)
        assert not workspace.vendored

    def test_walks_up_from_subdirectory(self, tmp_path: Path) -> None:
        worktree = _make_standalone_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        (worktree / "src" / "teatree").mkdir(parents=True)

        assert _resolve_teatree_workspace(worktree / "src" / "teatree").root == worktree

    def test_refuses_main_clone(self, tmp_path: Path) -> None:
        clone = tmp_path / "souliane" / "teatree"
        clone.mkdir(parents=True)
        (clone / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        (clone / ".git").mkdir()

        with pytest.raises(OverlayDevError, match="main clone"):
            _resolve_teatree_workspace(clone)

    def test_refuses_non_teatree_dir(self, tmp_path: Path) -> None:
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "pyproject.toml").write_text('[project]\nname = "other"\n')
        (other / ".git").write_text("gitdir: /fake\n")

        with pytest.raises(OverlayDevError, match="not a teatree"):
            _resolve_teatree_workspace(other)

    def test_raises_when_no_pyproject_found(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        with pytest.raises(OverlayDevError, match="No teatree workspace"):
            _resolve_teatree_workspace(empty)

    def test_teatree_source_with_no_git_marker_anywhere_is_named(self, tmp_path: Path) -> None:
        # Not vendored, no enclosing workspace — the specific diagnostic survives.
        orphan = tmp_path / "wt"
        orphan.mkdir()
        (orphan / "pyproject.toml").write_text('[project]\nname = "teatree"\n')

        with pytest.raises(OverlayDevError, match=r"no \.git marker"):
            _resolve_teatree_workspace(orphan)


class TestResolveVendoredWorkspace:
    def test_resolves_fork_root_from_fork_root(self, tmp_path: Path) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork")

        workspace = _resolve_teatree_workspace(fork)

        assert workspace == TeatreeWorkspace(root=fork, core=fork / "vendor" / "teatree")
        assert workspace.vendored

    def test_resolves_fork_root_from_inside_vendored_core(self, tmp_path: Path) -> None:
        # The regression that made the command unusable in a fork: walking up from
        # vendored core hit teatree's own pyproject with no `.git` beside it.
        fork = _make_vendored_fork(tmp_path / "acme-fork")
        deep = fork / "vendor" / "teatree" / "src" / "teatree"
        deep.mkdir(parents=True)

        assert _resolve_teatree_workspace(deep).root == fork

    def test_resolves_fork_root_from_an_unrelated_subdirectory(self, tmp_path: Path) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork")
        (fork / "src" / "acme_overlay").mkdir(parents=True)

        assert _resolve_teatree_workspace(fork / "src" / "acme_overlay").root == fork

    def test_accepts_the_main_clone(self, tmp_path: Path) -> None:
        # A fork is edited IN PLACE, so its main clone is the sanctioned workspace.
        # The sibling-worktree hazard the standalone refusal guards is unreachable
        # here (a co-located overlay creates no sibling) and is guarded at its site.
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="dir")

        assert _resolve_teatree_workspace(fork).is_main_clone

    def test_accepts_a_fork_worktree(self, tmp_path: Path) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork-wt", git_marker="file")

        workspace = _resolve_teatree_workspace(fork)

        assert workspace.root == fork
        assert not workspace.is_main_clone

    def test_requires_a_git_boundary_at_the_fork_root(self, tmp_path: Path) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="none")

        with pytest.raises(OverlayDevError, match=r"no \.git marker"):
            _resolve_teatree_workspace(fork)

    def test_vendor_parent_without_a_pyproject_is_not_a_workspace(self, tmp_path: Path) -> None:
        # Mirrors deploy/t3's own detection: a vendor parent with no pyproject is
        # not a host project, so it must not be claimed as the git boundary.
        bare = tmp_path / "bare"
        core = bare / "vendor" / "teatree"
        core.mkdir(parents=True)
        (core / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        (bare / ".git").mkdir()

        with pytest.raises(OverlayDevError, match="No teatree workspace"):
            _resolve_teatree_workspace(bare)


class TestWorkspaceDeclaresOverlay:
    def test_true_when_the_fork_declares_the_entry_point(self, tmp_path: Path) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork")

        assert _workspace_declares_overlay(_resolve_teatree_workspace(fork), "example-overlay")

    def test_false_for_an_overlay_the_fork_does_not_declare(self, tmp_path: Path) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork")

        assert not _workspace_declares_overlay(_resolve_teatree_workspace(fork), "other-overlay")

    def test_false_for_a_standalone_teatree_worktree(self, tmp_path: Path) -> None:
        worktree = _make_standalone_worktree(tmp_path / "ticket" / "teatree")

        assert not _workspace_declares_overlay(_resolve_teatree_workspace(worktree), "example-overlay")


class TestOverlayResolvesInside:
    def _workspace(self, tmp_path: Path) -> TeatreeWorkspace:
        return _resolve_teatree_workspace(_make_vendored_fork(tmp_path / "acme-fork"))

    def test_true_when_the_module_lives_in_the_workspace(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)
        origin = workspace.root / "src" / "acme_overlay" / "__init__.py"
        origin.parent.mkdir(parents=True)
        origin.touch()

        with (
            patch("teatree.cli.overlay_dev.entry_points", return_value=[_entry_point()]),
            patch("importlib.util.find_spec", return_value=SimpleNamespace(origin=str(origin))),
        ):
            assert _overlay_resolves_inside(workspace, "example-overlay")

    def test_false_when_the_module_lives_elsewhere(self, tmp_path: Path) -> None:
        # A fork WORKTREE on the host: the module still resolves to the main
        # clone, so the editable install is genuinely needed.
        workspace = self._workspace(tmp_path)
        elsewhere = tmp_path / "main-clone" / "src" / "acme_overlay" / "__init__.py"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.touch()

        with (
            patch("teatree.cli.overlay_dev.entry_points", return_value=[_entry_point()]),
            patch("importlib.util.find_spec", return_value=SimpleNamespace(origin=str(elsewhere))),
        ):
            assert not _overlay_resolves_inside(workspace, "example-overlay")

    def test_false_when_the_overlay_is_not_installed(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)

        with patch("teatree.cli.overlay_dev.entry_points", return_value=[]):
            assert not _overlay_resolves_inside(workspace, "example-overlay")

    def test_false_when_the_module_cannot_be_located(self, tmp_path: Path) -> None:
        workspace = self._workspace(tmp_path)

        with (
            patch("teatree.cli.overlay_dev.entry_points", return_value=[_entry_point()]),
            patch("importlib.util.find_spec", side_effect=ImportError("no such module")),
        ):
            assert not _overlay_resolves_inside(workspace, "example-overlay")


class TestResolveOverlaySource:
    def test_resolves_from_registry_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        main_clone = tmp_path / "acme-workspace" / "example-overlay"
        main_clone.mkdir(parents=True)
        db = tmp_path / "config.sqlite3"
        _seed_cold_registry(db, {"example-overlay": {"path": str(main_clone)}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        assert _resolve_overlay_source("example-overlay") == main_clone

    def test_raises_when_overlay_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "config.sqlite3"
        _seed_cold_registry(db, {})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        with pytest.raises(OverlayDevError, match="not configured"):
            _resolve_overlay_source("ghost-overlay-nobody-registers")

    def test_raises_when_path_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "config.sqlite3"
        _seed_cold_registry(db, {"example-overlay": {"class": "foo:Bar"}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        with pytest.raises(OverlayDevError, match="no path configured"):
            _resolve_overlay_source("example-overlay")


class TestEnsureSiblingWorktree:
    def test_returns_existing_sibling(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        workspace = _resolve_teatree_workspace(_make_standalone_worktree(ticket_dir / "teatree"))
        sibling = ticket_dir / "example-overlay"
        sibling.mkdir()
        main_clone = tmp_path / "main" / "example-overlay"
        main_clone.mkdir(parents=True)

        assert _ensure_sibling_worktree(workspace, main_clone, branch="any") == sibling

    def test_creates_sibling_via_git_worktree_add(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        workspace = _resolve_teatree_workspace(_make_standalone_worktree(ticket_dir / "teatree"))
        main_clone = tmp_path / "main" / "example-overlay"
        main_clone.mkdir(parents=True)

        with patch("teatree.utils.run.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = _ensure_sibling_worktree(workspace, main_clone, branch="ac-teatree-120")

        assert result == ticket_dir / "example-overlay"
        cmds = [call.args[0] for call in run.call_args_list]
        add_cmd = next(c for c in cmds if "worktree" in c and "add" in c)
        assert str(ticket_dir / "example-overlay") in add_cmd

    def test_falls_back_to_default_branch_when_branch_missing(self, tmp_path: Path) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        workspace = _resolve_teatree_workspace(_make_standalone_worktree(ticket_dir / "teatree"))
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

        with patch("teatree.utils.run.subprocess.run", side_effect=fake_run):
            _ensure_sibling_worktree(workspace, main_clone, branch="missing-branch")

        add_cmd = next(c for c in calls if "add" in c)
        assert "missing-branch" not in add_cmd
        assert "development" in add_cmd

    def test_refuses_to_create_a_sibling_beside_a_main_clone(self, tmp_path: Path) -> None:
        # The hazard the standalone refusal always guarded, now enforced at its
        # site so the vendored layout can accept a main clone without losing it:
        # `git worktree add` here would target the overlay's OWN main clone.
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="dir")
        main_clone = tmp_path / "main" / "other-overlay"
        main_clone.mkdir(parents=True)

        with pytest.raises(OverlayDevError, match="main clone"):
            _ensure_sibling_worktree(_resolve_teatree_workspace(fork), main_clone, branch="any")


class TestUvPipInstall:
    def test_no_deps_precedes_editable_so_uv_can_parse_it(self, tmp_path: Path) -> None:
        # `--editable` takes a VALUE: `--editable --no-deps <path>` makes uv
        # consume the flag as that value and refuse the whole command.
        workspace_root = tmp_path / "teatree"
        workspace_root.mkdir()
        overlay = tmp_path / "example-overlay"
        overlay.mkdir()

        with patch("teatree.utils.run.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            _uv_pip_install_editable(workspace_root, overlay)

        cmd = run.call_args.args[0]
        assert cmd == ["uv", "pip", "install", "--no-deps", "--editable", str(overlay)]
        assert str(run.call_args.kwargs["cwd"]) == str(workspace_root)


class TestInstallCommand:
    def test_standalone_layout_installs_a_sibling_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticket_dir = tmp_path / "ac-teatree-120-xyz"
        teatree_wt = _make_standalone_worktree(ticket_dir / "teatree")
        main_clone = tmp_path / "workspace" / "example-overlay"
        main_clone.mkdir(parents=True)
        db = tmp_path / "config.sqlite3"
        _seed_cold_registry(db, {"example-overlay": {"path": str(main_clone)}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(teatree_wt)

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="main\n", stderr="")

        with patch("teatree.utils.run.subprocess.run", side_effect=fake_run):
            result = CliRunner().invoke(overlay_dev_app, ["install", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert any(("worktree" in c and "add" in c) for c in captured)
        install_cmd = next(c for c in captured if "uv" in c and "install" in c)
        assert install_cmd == [
            "uv",
            "pip",
            "install",
            "--no-deps",
            "--editable",
            str(ticket_dir / "example-overlay"),
        ]
        state = json.loads((teatree_wt / ".t3.local.json").read_text())
        assert state["overlays"]["example-overlay"]["source"] == str(ticket_dir / "example-overlay")

    def test_vendored_fork_installs_the_workspace_itself_without_a_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="file")
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(fork)

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="main\n", stderr="")

        with (
            patch("teatree.utils.run.subprocess.run", side_effect=fake_run),
            patch("teatree.cli.overlay_dev.entry_points", return_value=[]),
        ):
            result = CliRunner().invoke(overlay_dev_app, ["install", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert not any(("worktree" in c and "add" in c) for c in captured)
        assert captured == [["uv", "pip", "install", "--no-deps", "--editable", str(fork)]]
        state = json.loads((fork / ".t3.local.json").read_text())
        assert state["overlays"]["example-overlay"]["source"] == str(fork)

    def test_vendored_fork_reports_already_provided_and_installs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The containerized case: core AND the overlay are already installed
        # editable from this very tree, so there is nothing left to do.
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="dir")
        origin = fork / "src" / "acme_overlay" / "__init__.py"
        origin.parent.mkdir(parents=True)
        origin.touch()
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(fork)

        captured: list[list[str]] = []

        with (
            patch("teatree.utils.run.subprocess.run", side_effect=lambda cmd, **_k: captured.append(cmd)),
            patch("teatree.cli.overlay_dev.entry_points", return_value=[_entry_point()]),
            patch("importlib.util.find_spec", return_value=SimpleNamespace(origin=str(origin))),
        ):
            result = CliRunner().invoke(overlay_dev_app, ["install", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert "already provided by this workspace" in result.output
        assert captured == []
        state = json.loads((fork / ".t3.local.json").read_text())
        assert state["overlays"]["example-overlay"]["source"] == str(fork)

    def test_reads_the_declared_invocation_cwd_rather_than_the_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The containerized boundary: the process runs from a directory with no
        # workspace above it, and only the declared cwd finds the fork.
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="dir")
        elsewhere = tmp_path / "container-home"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(fork))

        with (
            patch("teatree.utils.run.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
            patch("teatree.cli.overlay_dev.entry_points", return_value=[]),
        ):
            result = CliRunner().invoke(overlay_dev_app, ["install", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert str(fork) in result.output

    def test_without_the_declared_cwd_the_same_invocation_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The CONTROL for the test above: same fork, same process cwd, no declared
        # cwd — this is the reported bug, and it must still reproduce.
        _make_vendored_fork(tmp_path / "acme-fork", git_marker="dir")
        elsewhere = tmp_path / "container-home"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)

        result = CliRunner().invoke(overlay_dev_app, ["install", "example-overlay"])

        assert result.exit_code == 1
        assert "No teatree workspace found" in result.output


class TestUninstallCommand:
    def test_uninstall_removes_editable_and_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        teatree_wt = _make_standalone_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        (teatree_wt / ".t3.local.json").write_text('{"overlays": {"example-overlay": {"source": "/tmp/x"}}}')
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(teatree_wt)

        captured: list[list[str]] = []

        with patch("teatree.utils.run.subprocess.run", side_effect=lambda cmd, **_k: captured.append(cmd)):
            result = CliRunner().invoke(overlay_dev_app, ["uninstall", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert any(("uv" in c and "uninstall" in c and "example-overlay" in c) for c in captured)
        state = json.loads((teatree_wt / ".t3.local.json").read_text())
        assert "example-overlay" not in state["overlays"]

    def test_uninstall_works_in_a_vendored_fork(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="dir")
        (fork / ".t3.local.json").write_text('{"overlays": {"example-overlay": {"source": "/tmp/x"}}}')
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(fork)

        with patch("teatree.utils.run.subprocess.run", return_value=MagicMock(returncode=0)):
            result = CliRunner().invoke(overlay_dev_app, ["uninstall", "example-overlay"])

        assert result.exit_code == 0, result.output
        assert json.loads((fork / ".t3.local.json").read_text())["overlays"] == {}


class TestStatusCommand:
    def test_status_lists_installed_overlays(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        teatree_wt = _make_standalone_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        (teatree_wt / ".t3.local.json").write_text(
            '{"overlays": {"example-overlay": {"source": "/tmp/example-overlay"}}}'
        )
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(teatree_wt)

        result = CliRunner().invoke(overlay_dev_app, ["status"])

        assert result.exit_code == 0, result.output
        assert "example-overlay" in result.output
        assert "/tmp/example-overlay" in result.output

    def test_status_reports_none_when_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        teatree_wt = _make_standalone_worktree(tmp_path / "ac-teatree-120-xyz" / "teatree")
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(teatree_wt)

        result = CliRunner().invoke(overlay_dev_app, ["status"])

        assert result.exit_code == 0
        assert "No overlays installed" in result.output

    def test_status_reads_the_fork_root_state_from_inside_vendored_core(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fork = _make_vendored_fork(tmp_path / "acme-fork", git_marker="dir")
        (fork / ".t3.local.json").write_text('{"overlays": {"example-overlay": {"source": "/tmp/acme"}}}')
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(fork / "vendor" / "teatree")

        result = CliRunner().invoke(overlay_dev_app, ["status"])

        assert result.exit_code == 0, result.output
        assert "example-overlay" in result.output


class TestErrorHandling:
    @pytest.mark.parametrize("command", [["install", "missing"], ["uninstall", "missing"], ["status"]])
    def test_reports_overlay_dev_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
    ) -> None:
        empty = tmp_path / "elsewhere"
        empty.mkdir()
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.chdir(empty)

        result = CliRunner().invoke(overlay_dev_app, command)

        assert result.exit_code == 1
        assert "Error" in result.output

    def test_default_branch_falls_back_to_main(self, tmp_path: Path) -> None:
        from teatree.cli.overlay_dev import _default_branch  # noqa: PLC0415

        with patch("teatree.cli.overlay_dev.run_allowed_to_fail") as run_mock:
            run_mock.return_value = MagicMock(returncode=128, stdout="", stderr="not a symbolic ref")
            assert _default_branch(tmp_path) == "main"
