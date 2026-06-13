"""External/private-test repo resolution and Playwright env construction.

Split out of ``e2e.py`` (mirroring the ``_e2e_discovery`` and
``_test_plan`` splits) to keep that module under the project's per-file
LOC cap. These are the pure helpers the ``external``/``project`` runners
lean on: cloning the external test repo, resolving the private-tests
directory, and building the Playwright environment dict.
"""

import os
from pathlib import Path

from teatree.config import E2ERepo
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import _find_env_cache, _get_user_cwd, _parse_env_file
from teatree.paths import get_data_dir
from teatree.utils.run import run_checked


def clone_or_update_e2e_repo(repo: E2ERepo) -> Path:
    """Clone or update an external E2E repo to the local cache and return the playwright root.

    On first run: ``git clone --branch <branch> --depth 1 <url> <cache_path>``.
    On subsequent runs: ``git fetch origin <branch>`` + ``git reset --hard FETCH_HEAD``.

    Returns ``cache_path / repo.e2e_dir`` — the directory passed as ``cwd`` to Playwright.
    """
    cache_path = get_data_dir("e2e-repos") / repo.name
    if not cache_path.exists():
        run_checked(
            ["git", "clone", "--branch", repo.branch, "--depth", "1", repo.url, str(cache_path)],
        )
    else:
        run_checked(["git", "-C", str(cache_path), "fetch", "origin", repo.branch])
        run_checked(["git", "-C", str(cache_path), "reset", "--hard", "FETCH_HEAD"])
    return cache_path / repo.e2e_dir


def resolve_private_tests_path() -> Path | None:
    """Resolve the private tests directory from env or config."""
    from teatree.config import load_config  # noqa: PLC0415

    private_tests = os.environ.get("T3_PRIVATE_TESTS", "")
    if not private_tests:
        private_tests = load_config().raw.get("teatree", {}).get("private_tests", "")
    if not private_tests:
        return None
    path = Path(private_tests).expanduser()
    return path if path.is_dir() else None


def build_e2e_env(
    frontend_url: str | None = None,
    *,
    headed: bool,
    target: str,
    compose_project: str | None = None,
    env_cache_override: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build environment dict for Playwright: ``BASE_URL``, overlay extras, ``CI``.

    When *frontend_url* is given it overrides ``BASE_URL``.
    When it is ``None`` the existing ``BASE_URL`` env var is preserved (DEV / staging mode).

    *target* is the resolved dual-env target (``"dev"`` or ``"local"``); it is
    exported as ``T3_E2E_TARGET`` so a single dual-mode spec can branch on
    ``process.env.T3_E2E_TARGET === 'dev'`` instead of re-deriving the target
    from a ``BASE_URL`` host regex.

    *compose_project* is the teatree-managed docker-compose project of the
    resolved worktree (``compose_project(worktree)``) for a local target. It
    is exported as ``COMPOSE_PROJECT_NAME`` — the variable ``docker compose``
    natively honours — so a spec that resolves the backend via a bare
    ``docker compose port web 8000`` / ``docker compose exec -T web`` (run
    from the backend repo dir, no ``-p``) deterministically targets the
    teatree stack whose ``web`` container has the restored-Postgres
    ``DATABASE_URL`` injected, instead of defaulting to the directory
    basename and missing it. ``None`` (dev target) leaves it unset.

    Overlay-specific env vars (e.g. ``CUSTOMER``) come from
    :meth:`OverlayBase.get_e2e_env_extras` — core only knows about ``BASE_URL``,
    ``T3_E2E_TARGET``, ``COMPOSE_PROJECT_NAME`` and ``CI``.
    """
    env = {**os.environ}
    if frontend_url is not None:
        env["BASE_URL"] = frontend_url
    env["T3_E2E_TARGET"] = target
    if compose_project:
        env["COMPOSE_PROJECT_NAME"] = compose_project

    if env_cache_override is not None:
        env_cache = env_cache_override
    else:
        envfile = _find_env_cache(_get_user_cwd())
        env_cache = _parse_env_file(envfile) if envfile is not None else {}
    for key, value in get_overlay().get_e2e_env_extras(env_cache).items():
        env.setdefault(key, value)

    if headed:
        env.pop("CI", None)
    else:
        env["CI"] = "1"
    return env
