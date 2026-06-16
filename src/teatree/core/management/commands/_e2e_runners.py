"""External/private-test repo resolution and Playwright env construction.

Split out of ``e2e.py`` (mirroring the ``_e2e_discovery`` and
``_test_plan`` splits) to keep that module under the project's per-file
LOC cap. These are the pure helpers the ``external``/``project`` runners
lean on: cloning the external test repo, resolving the private-tests
directory, and building the Playwright environment dict.
"""

import os
from pathlib import Path

import typer

from teatree.config import E2ERepo, load_e2e_repos
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import _find_env_cache, _get_user_cwd, _parse_env_file
from teatree.paths import get_data_dir
from teatree.utils.run import CommandFailedError, run_checked

_BRANCH_HELP = "Specs git ref, overriding the [e2e_repos.<name>].branch default (e.g. an open MR's branch)."
BRANCH_OPTION = typer.Option("", "--branch", "--ref", help=_BRANCH_HELP)


class E2eBranchNotFoundError(RuntimeError):
    """The requested E2E specs ref does not exist on the remote."""

    def __init__(self, *, name: str, ref: str, url: str) -> None:
        super().__init__(
            f"E2E specs branch '{ref}' not found on repo '{name}' ({url}). "
            "Pass an existing --branch/--ref, or check the open MR's source branch name.",
        )
        self.ref = ref


class E2eSpecsResolutionError(RuntimeError):
    """The external specs working directory could not be resolved; carries the CLI exit code."""

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code

    @classmethod
    def repo_not_in_config(cls, repo: str) -> "E2eSpecsResolutionError":
        return cls(f"E2E repo '{repo}' not found in ~/.teatree.toml [e2e_repos].", exit_code=1)

    @classmethod
    def branch_needs_repo(cls) -> "E2eSpecsResolutionError":
        msg = "--branch/--ref applies only to a --repo clone; T3_PRIVATE_TESTS is checked out by you."
        return cls(msg, exit_code=2)

    @classmethod
    def no_private_tests(cls) -> "E2eSpecsResolutionError":
        msg = "private_tests not configured in ~/.teatree.toml / T3_PRIVATE_TESTS, or directory missing."
        return cls(msg, exit_code=1)


def clone_or_update_e2e_repo(repo: E2ERepo, branch_override: str = "") -> Path:
    """Clone or update an external E2E repo to the local cache and return the playwright root.

    The ref is *branch_override* when given, else ``repo.branch`` (the
    ``[e2e_repos.<name>].branch`` config default). ``branch_override`` lets the
    suite run from a working branch (e.g. an open MR) instead of the default.

    On first run: ``git clone --branch <ref> --depth 1 <url> <cache_path>``.
    On subsequent runs: ``git fetch origin <ref>`` + ``git reset --hard FETCH_HEAD``.

    Raises :class:`E2eBranchNotFoundError` when the ref does not exist on the
    remote, so a typo'd or stale branch fails with a clear message rather than
    an opaque git error.

    Returns ``cache_path / repo.e2e_dir`` — the directory passed as ``cwd`` to Playwright.
    """
    ref = branch_override or repo.branch
    cache_path = get_data_dir("e2e-repos") / repo.name
    try:
        if not cache_path.exists():
            run_checked(["git", "clone", "--branch", ref, "--depth", "1", repo.url, str(cache_path)])
        else:
            run_checked(["git", "-C", str(cache_path), "fetch", "origin", ref])
            run_checked(["git", "-C", str(cache_path), "reset", "--hard", "FETCH_HEAD"])
    except CommandFailedError as exc:
        raise E2eBranchNotFoundError(name=repo.name, ref=ref, url=repo.url) from exc
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


def resolve_external_specs_path(repo: str, branch: str) -> Path:
    """Resolve the Playwright working directory for the ``external`` runner.

    ``--repo <name>`` clones the configured ``[e2e_repos.<name>]`` at *branch*
    (or its default); otherwise the ``T3_PRIVATE_TESTS`` directory is used.
    *branch* is only meaningful for the clone path — a ``T3_PRIVATE_TESTS``
    directory is checked out by the user, so a branch there is a misuse.

    Raises :class:`E2eSpecsResolutionError` (carrying the CLI exit code) on any
    misconfiguration so the caller maps one exception to one ``SystemExit``.
    """
    if repo:
        repos_by_name = {r.name: r for r in load_e2e_repos()}
        if repo not in repos_by_name:
            raise E2eSpecsResolutionError.repo_not_in_config(repo)
        try:
            return clone_or_update_e2e_repo(repos_by_name[repo], branch)
        except E2eBranchNotFoundError as exc:
            raise E2eSpecsResolutionError(str(exc), exit_code=1) from exc
    if branch:
        raise E2eSpecsResolutionError.branch_needs_repo()
    private_tests_path = resolve_private_tests_path()
    if not private_tests_path:
        raise E2eSpecsResolutionError.no_private_tests()
    return private_tests_path


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
