"""Shared fixtures for teatree script tests."""

import importlib.util
import os
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure unit tests use the settings declared in pyproject.toml, not a stale
# DJANGO_SETTINGS_MODULE from the shell. pytest-django falls back to
# pyproject.toml when the env var is absent.
os.environ.pop("DJANGO_SETTINGS_MODULE", None)
# Pin T3_OVERLAY_NAME to the in-repo overlay so tests stay deterministic even
# when extra overlays are editable-installed for dogfooding (see #120). Tests
# that exercise overlay resolution override via monkeypatch.setenv/delenv.
os.environ["T3_OVERLAY_NAME"] = "t3-teatree"

# Guard against import-time side effects in script modules that call _init.init()
# at module import. Route HOME/T3_WORKSPACE_DIR to a disposable temp sandbox.
_IMPORT_SANDBOX = tempfile.TemporaryDirectory(prefix="teatree-tests-import-")
_IMPORT_HOME = Path(_IMPORT_SANDBOX.name) / "home"
_IMPORT_WORKSPACE = _IMPORT_HOME / "workspace"
_IMPORT_WORKSPACE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HOME", str(_IMPORT_HOME))
os.environ.setdefault("T3_WORKSPACE_DIR", str(_IMPORT_WORKSPACE))


# Config-source controls. Stripping these would defeat reproducing the CI
# default-branch condition locally: the CI image's git defaults to ``master``,
# so a fixture that assumes ``main`` exits 128. ``GIT_CONFIG_NOSYSTEM=1`` forces
# git's compiled-in ``master`` default on a dev box whose system/global config
# bakes in ``main`` (souliane/teatree#2359). These do not carry the parent
# repo's index/worktree the way the hook vars below do, so they are safe to keep.
_GIT_CONFIG_SOURCE_VARS = frozenset(
    {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"},
)


def _strip_git_hook_env() -> None:
    """Strip GIT_* env vars inherited from pre-commit hooks.

    When pytest runs as a prek/pre-commit hook via ``git commit -a``, git sets
    ``GIT_INDEX_FILE`` to ``.git/index.lock``. Hook subprocesses inherit this,
    so any git operation in a test (e.g. ``git init`` in a temp dir) corrupts
    the parent repo's index. Stripping the hook ``GIT_*`` vars at session start
    prevents this. See https://github.com/j178/prek/issues/1786.

    Config-source controls (``GIT_CONFIG_NOSYSTEM`` and friends) are preserved
    so the CI default-branch condition is reproducible locally.
    """
    for var in list(os.environ):
        if var.startswith("GIT_") and var not in _GIT_CONFIG_SOURCE_VARS:
            del os.environ[var]


_strip_git_hook_env()


def load_script(name: str) -> types.ModuleType:
    """Dynamically load a teatree script as a module for testing."""
    p = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_mod", p)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_ok(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    """Create a MagicMock simulating a successful subprocess.run result."""
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set standard Postgres env vars for testing."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_USER", "testuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "testpass")


@pytest.fixture(autouse=True)
def _clear_backend_caches() -> Iterator[None]:
    """Clear caches and block real token resolution so tests never call gpg/pass.

    ``backend_factory.reset_backend_caches`` (not the partial
    ``loader.reset_backend_caches``) is used because only the former
    also clears ``backend_factory._messaging_cache`` — otherwise a test
    that builds a real messaging backend leaks it under the empty-overlay
    key and a later ``notify_user`` (no explicit backend) reuses it,
    reaching a real ``pass`` subprocess. ``read_pass`` is patched on
    BOTH ``teatree.utils.secrets`` and the name already bound into
    ``teatree.backends.loader`` (``from … import read_pass``) — patching
    only the source module misses loader's bound reference.
    """
    from unittest.mock import patch  # noqa: PLC0415

    import teatree.backends.loader as _loader_mod  # noqa: PLC0415
    import teatree.utils.secrets as _secrets_mod  # noqa: PLC0415
    from teatree.core.backend_factory import reset_backend_caches  # noqa: PLC0415
    from teatree.core.overlay_loader import reset_overlay_cache  # noqa: PLC0415

    def _no_pass(_key: str) -> str:
        return ""

    reset_backend_caches()
    reset_overlay_cache()
    with (
        patch.object(_secrets_mod, "read_pass", _no_pass),
        patch.object(_loader_mod, "read_pass", _no_pass),
    ):
        yield
    reset_backend_caches()
    reset_overlay_cache()


@pytest.fixture(autouse=True)
def _reset_webhook_rate_limiter() -> Iterator[None]:
    """Drop the process-singleton webhook limiter so buckets don't leak across tests."""
    from teatree.core.views._rate_limit import reset_webhook_rate_limiter  # noqa: PLC0415

    reset_webhook_rate_limiter()
    yield
    reset_webhook_rate_limiter()


@pytest.fixture(autouse=True)
def _unset_review_skill_by_default() -> Iterator[None]:
    """Pin the #1539 reviewing-phase gate to its NO-OP unless a test opts in.

    The effective ``review_skill`` resolves through the host ``~/.teatree.toml``
    (not Django settings), so a developer who configures one would otherwise see
    every reviewing-phase test refuse. Tests that exercise the gate re-patch
    ``configured_review_skill`` with a non-empty value inside their own scope.
    """
    from unittest.mock import patch  # noqa: PLC0415

    with patch("teatree.core.gates.review_skill_gate.configured_review_skill", return_value=""):
        yield


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace structure with a main repo."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    repo = ws / "my-project"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "manage.py").touch()

    return ws


@pytest.fixture
def project_info():
    """Shared ProjectInfo for tests that mock GitLab API calls."""
    from lib.gitlab import ProjectInfo  # noqa: PLC0415

    return ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")


@pytest.fixture
def ticket_dir(workspace: Path) -> Path:
    """Create a ticket directory with a worktree inside the workspace."""
    td = workspace / "my-project-1234-test-fix"
    td.mkdir()

    wt = td / "my-project"
    wt.mkdir()
    # In worktrees, .git is a file (not a directory)
    (wt / ".git").write_text("gitdir: /some/path/.git/worktrees/my-project")

    return td


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Clear the extension point registry between tests (legacy scripts only)."""
    try:
        from lib.registry import clear  # noqa: PLC0415
    except ImportError:
        yield
        return
    clear()
    yield
    clear()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Prefer django.test.TestCase for DB-heavy tests; allow @pytest.mark.django_db on classes.

    Standalone functions with @pytest.mark.django_db should be grouped into
    TestCase classes when they share setup data (setUpTestData).  Class-based
    tests may use either TestCase or @pytest.mark.django_db depending on
    whether they need pytest fixtures (monkeypatch, tmp_path).

    See: souliane/teatree#98
    """
    for item in items:
        marker = item.get_closest_marker("django_db")
        if marker is None:
            continue
        cls = getattr(item, "cls", None)
        if cls is not None:
            continue  # class-based tests may use either pattern
        pytest.fail(
            f"{item.nodeid}: Standalone @pytest.mark.django_db functions "
            f"should be grouped into a TestCase class (see souliane/teatree#98)",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate process env so tests cannot touch host workspace/config.

    ``$HOME`` alone is not enough: ``teatree.config.CONFIG_PATH`` is bound to
    ``Path.home() / ".teatree.toml"`` ONCE at import (before any fixture runs),
    so a later ``$HOME`` redirect leaves the suite reading the developer's real
    ``~/.teatree.toml``. A real ``check_updates`` flag in that host config let
    the ``[update] …`` banner prepend non-JSON to a CLI's stdout under the
    previously default ``--exitfirst`` masked by ``-n auto``. That host-config
    leak is closed two ways: the facade ``CONFIG_PATH`` is redirected at a hermetic
    per-test file (no keys → all TOML-home settings default), and ``check_updates``
    is DB-home (eliminate-~/.teatree.toml: ``check_for_updates`` reads it via the
    Django-free ``cold_reader``), so under the isolated ``$HOME`` below — with no
    canonical config DB — it fails open to its dataclass default, never the host
    value. (The former host ``[loops.review]
    enabled = false`` leak is moot since #2702 removed the ``[loops]`` toml
    read; the ``T3_LOOPS_DISABLED`` env leak below is still cleared per test.)
    """
    home = tmp_path / "home"
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Keep the hermetic config OUT of ``home`` — several tests rebuild
    # ``tmp_path / "home"`` themselves and assert on the presence/absence of
    # their OWN ``home/.teatree.toml``, so a file planted there would collide.
    config_dir = tmp_path / "t3-hermetic-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    hermetic_config = config_dir / ".teatree.toml"
    hermetic_config.write_text("[teatree]\n", encoding="utf-8")
    import teatree.config as _config  # noqa: PLC0415 — patched per-test, import lazily

    monkeypatch.setattr(_config, "CONFIG_PATH", hermetic_config)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    # The cold-hook readers now resolve the canonical ConfigSetting DB from these
    # (config-unify PR3): clear both so the flipped ``teatree_settings`` / statusline
    # readers resolve under the isolated ``$HOME`` and never read a host DB.
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    # Default to per-worktree postgres for test isolation (override in specific tests)
    monkeypatch.setenv("T3_SHARE_DB_SERVER", "false")
    monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("T3_BRANCH_PREFIX", raising=False)
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    monkeypatch.delenv("T3_ORIG_CWD", raising=False)
    monkeypatch.delenv("TICKET_DIR", raising=False)
    monkeypatch.delenv("WT_VARIANT", raising=False)
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    # Loop control is DB-only: ``review_loop_enabled`` reads the DB ``LoopState``
    # tier and no env var, so ``T3_LOOPS_DISABLED`` is inert — there is nothing
    # to isolate here (the env-inertness is pinned by
    # ``tests/teatree_loop/test_review_loop_db_only_control.py``).
