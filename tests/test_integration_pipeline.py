"""Integration tests: ticket -> setup -> finalize -> create_mr pipeline.

Exercises 2 parallel tickets with real git worktrees and file I/O.
Mocks only external tools: Docker, PostgreSQL, curl, GitLab API.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import load_script
from create_mr import main as create_mr_main
from lib.gitlab import ProjectInfo
from wt_finalize import wt_finalize
from wt_setup import wt_setup


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path, *, files: dict[str, str] | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "master")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@test.com")
    (path / "README.md").write_text("# Test\n")
    for name, content in (files or {}).items():
        (path / name).write_text(content)
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")


def _add_bare_remote(repo: Path, bare_dir: Path) -> None:
    subprocess.run(  # noqa: S603
        ["git", "clone", "--bare", str(repo), str(bare_dir)],  # noqa: S607
        capture_output=True,
        check=True,
    )
    _git(repo, "remote", "add", "origin", str(bare_dir))
    _git(repo, "fetch", "origin")


def _parse_env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Workspace with 2 real git repos + bare remotes."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    manage_py = 'import os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myapp.settings")'
    _init_repo(ws / "backend", files={"manage.py": manage_py, ".gitignore": ".python-version\n"})
    # .python-version is untracked (gitignored) so git worktree won't copy it — tests the symlink path
    (ws / "backend" / ".python-version").write_text("3.12.6")
    _init_repo(ws / "frontend", files={"package.json": '{"name": "fe"}'})

    for name in ("backend", "frontend"):
        _add_bare_remote(ws / name, ws / f".bare-{name}")

    monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("T3_BRANCH_PREFIX", "ac")
    return ws


@pytest.fixture
def two_tickets(pipeline_workspace: Path) -> tuple[Path, Path]:
    """Create worktrees for 2 tickets using real git."""
    mod = load_script("ws_ticket")
    assert mod.ws_ticket("1001", "add-login", ["backend", "frontend"]) == 0
    assert mod.ws_ticket("1002", "fix-logout", ["backend"]) == 0

    return (
        pipeline_workspace / "ac-backend-1001-add-login",
        pipeline_workspace / "ac-backend-1002-fix-logout",
    )


@pytest.fixture
def _registry() -> None:
    """Re-register extension points (cleared by autouse _clean_registry)."""
    from frameworks.django import register_django  # noqa: PLC0415
    from lib.extension_points import register_defaults  # noqa: PLC0415

    register_defaults()
    register_django()


@pytest.fixture
def setup_tickets(
    two_tickets: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    _registry: None,
) -> tuple[Path, Path]:
    """Run wt_setup for both tickets, mocking Docker/DB subprocess calls."""
    t1, t2 = two_tickets

    with (
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
        patch("wt_setup.db_exists", return_value=True),
    ):
        monkeypatch.chdir(t1 / "backend")
        assert wt_setup(variant="acme", ticket_url="https://gitlab.com/org/repo/-/issues/1001") == 0

        monkeypatch.chdir(t2 / "backend")
        assert wt_setup(variant="acme", ticket_url="https://gitlab.com/org/repo/-/issues/1002") == 0

    return t1, t2


# ---------------------------------------------------------------------------
# Tests: Worktree Creation
# ---------------------------------------------------------------------------


class TestWorktreeCreation:
    def test_directory_structure(self, two_tickets: tuple[Path, Path]) -> None:
        t1, t2 = two_tickets
        assert (t1 / "backend" / ".git").exists()
        assert (t1 / "frontend" / ".git").exists()
        assert (t2 / "backend" / ".git").exists()
        assert not (t2 / "frontend").exists()

    def test_worktree_branches(self, two_tickets: tuple[Path, Path]) -> None:
        t1, t2 = two_tickets
        assert _git(t1 / "backend", "branch", "--show-current").stdout.strip() == "ac-backend-1001-add-login"
        assert _git(t1 / "frontend", "branch", "--show-current").stdout.strip() == "ac-backend-1001-add-login"
        assert _git(t2 / "backend", "branch", "--show-current").stdout.strip() == "ac-backend-1002-fix-logout"


# ---------------------------------------------------------------------------
# Tests: Environment Isolation
# ---------------------------------------------------------------------------


class TestEnvironmentIsolation:
    def test_tickets_get_distinct_ports_and_names(self, setup_tickets: tuple[Path, Path]) -> None:
        t1, t2 = setup_tickets
        e1, e2 = _parse_env(t1 / ".env.worktree"), _parse_env(t2 / ".env.worktree")

        # Ports must differ between tickets
        for key in ("BACKEND_PORT", "FRONTEND_PORT", "POSTGRES_PORT"):
            assert e1[key] != e2[key], f"{key} must differ between tickets"

        # DB and compose names are ticket-specific
        assert e1["WT_DB_NAME"] == "wt_1001_acme"
        assert e2["WT_DB_NAME"] == "wt_1002_acme"
        assert e1["COMPOSE_PROJECT_NAME"] == "backend-wt1001"
        assert e2["COMPOSE_PROJECT_NAME"] == "backend-wt1002"

        # Redis is shared (single instance)
        assert e1["REDIS_PORT"] == e2["REDIS_PORT"] == "6379"

    def test_env_values_are_internally_consistent(self, setup_tickets: tuple[Path, Path]) -> None:
        t1, t2 = setup_tickets
        e1 = _parse_env(t1 / ".env.worktree")

        # URLs match ports
        assert e1["BACK_END_URL"] == f"http://localhost:{e1['BACKEND_PORT']}"
        assert e1["FRONT_END_URL"] == f"http://localhost:{e1['FRONTEND_PORT']}"

        # Django settings detected from manage.py
        assert e1["DJANGO_SETTINGS_MODULE"] == "myapp.settings"

        # Ticket URL stored
        e2 = _parse_env(t2 / ".env.worktree")
        assert e1["TICKET_URL"] == "https://gitlab.com/org/repo/-/issues/1001"
        assert e2["TICKET_URL"] == "https://gitlab.com/org/repo/-/issues/1002"

    def test_repo_env_symlinks_to_ticket_level(self, setup_tickets: tuple[Path, Path]) -> None:
        t1, t2 = setup_tickets
        for td in (t1, t2):
            link = td / "backend" / ".env.worktree"
            assert link.is_symlink()
            assert link.resolve() == (td / ".env.worktree").resolve()


# ---------------------------------------------------------------------------
# Tests: Full Pipeline (commit -> finalize -> create MR)
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_finalize_squashes_commits(
        self,
        setup_tickets: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        t1, _ = setup_tickets
        wt = t1 / "backend"

        (wt / "feature.py").write_text("def login(): pass\n")
        _git(wt, "add", "feature.py")
        _git(wt, "commit", "-m", "add login")

        (wt / "feature.py").write_text("def login(): return True\n")
        _git(wt, "add", "feature.py")
        _git(wt, "commit", "-m", "fix login")

        monkeypatch.chdir(wt)
        assert wt_finalize("feat: add login") == 0

        count = _git(wt, "rev-list", "--count", "origin/master..HEAD").stdout.strip()
        assert count == "1"
        msg = _git(wt, "log", "-1", "--format=%s").stdout.strip()
        assert msg == "feat: add login"

    def test_create_mr_dry_run(
        self,
        setup_tickets: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        t1, _ = setup_tickets
        wt = t1 / "backend"

        (wt / "feature.py").write_text("print('hello')\n")
        _git(wt, "add", "feature.py")
        _git(wt, "commit", "-m", "feat: add feature")

        monkeypatch.chdir(wt)
        monkeypatch.setenv("TICKET_DIR", str(t1))

        proj = ProjectInfo(project_id=42, path_with_namespace="org/backend", short_name="backend")
        with (
            patch("create_mr.resolve_project_from_remote", return_value=proj),
            patch("create_mr.current_user", return_value="testuser"),
        ):
            create_mr_main(str(wt), dry_run=True, skip_validation=True)

        out = capsys.readouterr().out
        assert "org/backend" in out
        assert "ac-backend-1001-add-login" in out
        assert "testuser" in out

    def test_create_mr_includes_ticket_url_in_title(
        self,
        setup_tickets: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        t1, _ = setup_tickets
        wt = t1 / "backend"

        (wt / "fix.py").write_text("# fix\n")
        _git(wt, "add", "fix.py")
        _git(wt, "commit", "-m", "fix: resolve login issue")

        monkeypatch.chdir(wt)
        monkeypatch.setenv("TICKET_DIR", str(t1))

        proj = ProjectInfo(project_id=42, path_with_namespace="org/backend", short_name="backend")
        with (
            patch("create_mr.resolve_project_from_remote", return_value=proj),
            patch("create_mr.current_user", return_value="testuser"),
        ):
            create_mr_main(str(wt), dry_run=True, skip_validation=True)

        out = capsys.readouterr().out
        assert "https://gitlab.com/org/repo/-/issues/1001" in out
