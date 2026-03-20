"""Tests for worktree lifecycle state machine."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from lib.fsm import InvalidTransitionError
from lib.lifecycle import WorktreeLifecycle, _direnv_load, _link_repo_env_worktree


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    td = tmp_path / "workspace" / "ac-1234"
    td.mkdir(parents=True)
    return td


@pytest.fixture
def lifecycle(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> WorktreeLifecycle:
    monkeypatch.setenv("T3_WORKSPACE_DIR", str(state_dir.parent))
    return WorktreeLifecycle(ticket_dir=str(state_dir))


@pytest.fixture
def _worktree_dirs(state_dir: Path) -> None:
    """Create main repo and worktree directories for provisioning tests."""
    ws = state_dir.parent
    main = ws / "my-project"
    main.mkdir(exist_ok=True)
    (main / ".git").mkdir(exist_ok=True)
    wt = state_dir / "my-project"
    wt.mkdir(exist_ok=True)


class TestStateFile:
    def test_initial_state_is_created(self, lifecycle: WorktreeLifecycle) -> None:
        assert lifecycle.state == "created"

    def test_save_creates_state_file(self, lifecycle: WorktreeLifecycle, state_dir: Path) -> None:
        lifecycle.state = "provisioned"
        lifecycle.facts = {"db_name": "test_db"}
        lifecycle.save()
        state_file = state_dir / ".state.json"
        assert state_file.is_file()
        data = json.loads(state_file.read_text())
        assert data["state"] == "provisioned"

    def test_save_skips_file_for_clean_created_state(self, lifecycle: WorktreeLifecycle, state_dir: Path) -> None:
        lifecycle.save()
        assert not (state_dir / ".state.json").is_file()

    def test_load_restores_state(self, state_dir: Path) -> None:
        state_file = state_dir / ".state.json"
        state_file.write_text(json.dumps({"state": "provisioned", "facts": {"ports": {"backend": 8001}}}))
        lc = WorktreeLifecycle(ticket_dir=str(state_dir))
        assert lc.state == "provisioned"
        assert lc.facts["ports"]["backend"] == 8001

    def test_load_missing_file_stays_created(self, state_dir: Path) -> None:
        lc = WorktreeLifecycle(ticket_dir=str(state_dir))
        assert lc.state == "created"
        assert lc.facts == {}


class TestTransitions:
    @pytest.mark.usefixtures("_worktree_dirs")
    def test_provision_from_created(
        self,
        lifecycle: WorktreeLifecycle,
        state_dir: Path,
    ) -> None:
        with (
            patch("lib.lifecycle.db_exists", return_value=False),
            patch("lib.lifecycle.find_free_ports", return_value=(8001, 4201, 5433, 6379)),
            patch("lib.lifecycle.registry") as mock_registry,
            patch("lib.lifecycle.subprocess.run"),
        ):
            mock_registry.call.return_value = True
            wt = str(state_dir / "my-project")
            main = str(state_dir.parent / "my-project")
            lifecycle.provision(wt_dir=wt, main_repo=main, variant="acme")

            assert lifecycle.state == "provisioned"
            assert lifecycle.facts["ports"]["backend"] == 8001
            assert lifecycle.facts["variant"] == "acme"
            assert lifecycle.facts["db_name"] == "wt_1234_acme"

            # .env.worktree created at ticket dir level
            envwt = state_dir / ".env.worktree"
            assert envwt.is_file()
            content = envwt.read_text()
            assert "WT_VARIANT=acme" in content
            assert "WT_DB_NAME=wt_1234_acme" in content
            assert "BACKEND_PORT=8001" in content

            # repo-level .env.worktree symlinks to ticket-dir
            repo_envwt = state_dir / "my-project" / ".env.worktree"
            assert repo_envwt.is_symlink()
            assert repo_envwt.resolve() == envwt.resolve()

            # extension points called: symlinks, env_extra, services, db_import, post_db
            ext_names = [c[0][0] for c in mock_registry.call.call_args_list]
            assert "wt_symlinks" in ext_names
            assert "wt_env_extra" in ext_names
            assert "wt_services" in ext_names
            assert "wt_db_import" in ext_names
            assert "wt_post_db" in ext_names

    @pytest.mark.usefixtures("_worktree_dirs")
    def test_provision_skips_import_when_db_exists(
        self,
        lifecycle: WorktreeLifecycle,
        state_dir: Path,
    ) -> None:
        with (
            patch("lib.lifecycle.db_exists", return_value=True),
            patch("lib.lifecycle.find_free_ports", return_value=(8001, 4201, 5433, 6379)),
            patch("lib.lifecycle.registry") as mock_registry,
            patch("lib.lifecycle.subprocess.run"),
        ):
            mock_registry.call.return_value = True
            wt = str(state_dir / "my-project")
            main = str(state_dir.parent / "my-project")
            lifecycle.provision(wt_dir=wt, main_repo=main, variant="")

            import_calls = [c for c in mock_registry.call.call_args_list if c[0][0] == "wt_db_import"]
            assert import_calls == []

    def test_cannot_start_services_from_created(self, lifecycle: WorktreeLifecycle) -> None:
        with pytest.raises(InvalidTransitionError, match="Cannot start_services from created"):
            lifecycle.start_services()

    def test_start_services_delegates_to_start_session(self, lifecycle: WorktreeLifecycle) -> None:
        with (
            patch("lib.lifecycle.registry") as mock_registry,
            patch("lib.lifecycle.subprocess.run"),
        ):
            lifecycle.state = "provisioned"
            lifecycle.facts = {"wt_dir": "/some/dir", "ports": {"backend": 8001, "frontend": 4201}}
            lifecycle.start_services()
            assert lifecycle.state == "services_up"
            session_calls = [c for c in mock_registry.call.call_args_list if c[0][0] == "wt_start_session"]
            assert len(session_calls) == 1

    def test_start_services_fallback_when_no_session_handler(
        self, lifecycle: WorktreeLifecycle, state_dir: Path
    ) -> None:
        (state_dir / ".env.worktree").write_text("WT_VARIANT=test\n")

        def side_effect(point: str, *args: object, **kwargs: object) -> object:  # noqa: ARG001
            if point == "wt_start_session":
                raise KeyError(point)
            return None

        with (
            patch("lib.lifecycle.registry") as mock_registry,
            patch("lib.lifecycle.subprocess.run"),
        ):
            mock_registry.call.side_effect = side_effect
            mock_registry.validate_overrides.return_value = None
            lifecycle.state = "provisioned"
            lifecycle.facts = {"wt_dir": "/some/dir", "ports": {"backend": 8001, "frontend": 4201}}
            lifecycle.start_services()
            assert lifecycle.state == "services_up"

    def test_start_services_skips_direnv_when_no_wt_dir(self, lifecycle: WorktreeLifecycle) -> None:
        with patch("lib.lifecycle.registry") as mock_registry:
            lifecycle.state = "provisioned"
            lifecycle.facts = {"ports": {"backend": 8001, "frontend": 4201}}
            lifecycle.start_services()
            assert lifecycle.state == "services_up"
            session_calls = [c for c in mock_registry.call.call_args_list if c[0][0] == "wt_start_session"]
            assert len(session_calls) == 1

    def test_verify_from_services_up(self, lifecycle: WorktreeLifecycle) -> None:
        lifecycle.state = "services_up"
        lifecycle.facts = {"ports": {"backend": 8001, "frontend": 4201}}
        lifecycle.verify()
        assert lifecycle.state == "ready"
        assert lifecycle.facts["urls"]["backend"] == "http://localhost:8001"

    def test_db_refresh_from_provisioned(self, lifecycle: WorktreeLifecycle) -> None:
        with patch("lib.lifecycle.registry") as mock_registry:
            lifecycle.state = "provisioned"
            lifecycle.facts = {"db_name": "wt_1234", "variant": "", "main_repo": "/repo", "wt_dir": "/wt"}
            lifecycle.db_refresh()
            assert lifecycle.state == "provisioned"
            assert mock_registry.call.call_count == 2  # wt_db_import + wt_post_db

    def test_db_refresh_skips_when_no_db_name(self, lifecycle: WorktreeLifecycle) -> None:
        with patch("lib.lifecycle.registry") as mock_registry:
            lifecycle.state = "provisioned"
            lifecycle.facts = {}
            lifecycle.db_refresh()
            assert mock_registry.call.call_count == 0

    def test_teardown_from_any_state(self, lifecycle: WorktreeLifecycle) -> None:
        lifecycle.teardown()
        assert lifecycle.state == "created"
        assert lifecycle.facts == {}

    def test_teardown_removes_state_file(self, lifecycle: WorktreeLifecycle, state_dir: Path) -> None:
        # Put lifecycle in a non-initial state so save() writes the file
        lifecycle.state = "provisioned"
        lifecycle.facts = {"ports": {"backend": 8001}}
        lifecycle.save()
        assert (state_dir / ".state.json").is_file()
        lifecycle.teardown()
        assert not (state_dir / ".state.json").is_file()

    def test_teardown_no_error_when_no_state_file(self, lifecycle: WorktreeLifecycle) -> None:
        lifecycle.teardown()  # Should not raise


class TestStatus:
    def test_status_returns_dict(self, lifecycle: WorktreeLifecycle) -> None:
        status = lifecycle.status()
        assert status["state"] == "created"
        assert "available_transitions" in status
        assert "facts" in status

    def test_available_transitions_from_created(self, lifecycle: WorktreeLifecycle) -> None:
        status = lifecycle.status()
        names = [t["method"] for t in status["available_transitions"]]
        assert "provision" in names
        assert "teardown" in names
        assert "start_services" not in names

    def test_available_transitions_from_provisioned(self, lifecycle: WorktreeLifecycle) -> None:
        lifecycle.state = "provisioned"
        status = lifecycle.status()
        names = [t["method"] for t in status["available_transitions"]]
        assert "start_services" in names
        assert "db_refresh" in names
        assert "teardown" in names
        assert "provision" not in names


class TestLinkRepoEnvWorktree:
    def test_replaces_existing_symlink(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        ticket_dir = tmp_path / "ticket"
        ticket_dir.mkdir()
        (ticket_dir / ".env.worktree").write_text("X=1\n", encoding="utf-8")
        (wt_dir / ".env.worktree").symlink_to(tmp_path / "nonexistent")

        _link_repo_env_worktree(str(wt_dir), str(ticket_dir))

        result = wt_dir / ".env.worktree"
        assert result.is_symlink()
        assert result.resolve() == (ticket_dir / ".env.worktree").resolve()

    def test_replaces_existing_file(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        ticket_dir = tmp_path / "ticket"
        ticket_dir.mkdir()
        (ticket_dir / ".env.worktree").write_text("X=1\n", encoding="utf-8")
        (wt_dir / ".env.worktree").write_text("old\n", encoding="utf-8")

        _link_repo_env_worktree(str(wt_dir), str(ticket_dir))

        result = wt_dir / ".env.worktree"
        assert result.is_symlink()


class TestDirenvLoad:
    def test_loads_env_from_direnv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_KEY", raising=False)
        with patch("lib.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": '{"MY_KEY": "hello"}'})()
            _direnv_load(str(tmp_path))

        assert os.environ["MY_KEY"] == "hello"

    def test_noop_when_direnv_returns_empty(self, tmp_path: Path) -> None:
        with patch("lib.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": ""})()
            _direnv_load(str(tmp_path))

    def test_noop_when_direnv_fails(self, tmp_path: Path) -> None:
        with patch("lib.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
            _direnv_load(str(tmp_path))
