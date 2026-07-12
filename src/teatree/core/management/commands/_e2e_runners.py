"""External/private-test repo resolution and Playwright env construction.

Split out of ``e2e.py`` (mirroring the ``_e2e_discovery`` and
``_test_plan`` splits) to keep that module under the project's per-file
LOC cap. These are the pure helpers the ``external``/``project`` runners
lean on: cloning the external test repo, resolving the private-tests
directory, and building the Playwright environment dict.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import typer

from teatree.config import E2ERepo, load_e2e_repos
from teatree.core.intake.resolve import _find_env_cache, _get_user_cwd, _parse_env_file
from teatree.core.overlay_loader import get_overlay
from teatree.paths import get_data_dir
from teatree.utils.run import CommandFailedError, run_checked

_BRANCH_HELP = "Specs git ref, overriding the [e2e_repos.<name>].branch default (e.g. an open MR's branch)."
BRANCH_OPTION = typer.Option("", "--branch", "--ref", help=_BRANCH_HELP)


@dataclass
class PlaywrightOptions:
    """Flags forwarded to the Playwright CLI."""

    test_path: str = ""
    update_snapshots: bool = False
    headed: bool = False
    extra: list[str] = field(default_factory=list)

    def to_args(self) -> list[str]:
        args: list[str] = []
        if self.test_path:
            args.append(self.test_path)
        args.append("--reporter=list")
        if self.update_snapshots:
            args.append("--update-snapshots")
        if self.headed:
            args.append("--headed")
        args.extend(self.extra)
        return args


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
        return cls(
            f"E2E repo '{repo}' not found in the e2e_repos config — "
            f"set it with `t3 <overlay> config_setting set e2e_repos <value>`.",
            exit_code=1,
        )

    @classmethod
    def branch_needs_repo(cls) -> "E2eSpecsResolutionError":
        msg = "--branch/--ref applies only to a --repo clone; T3_PRIVATE_TESTS is checked out by you."
        return cls(msg, exit_code=2)

    @classmethod
    def no_private_tests(cls) -> "E2eSpecsResolutionError":
        msg = (
            "private_tests not configured (set it with "
            "`t3 <overlay> config_setting set private_tests <path>` or the T3_PRIVATE_TESTS env var), "
            "or the directory is missing."
        )
        return cls(msg, exit_code=1)


@dataclass(frozen=True)
class E2eEnvContext:
    test_path: str = ""
    compose_project: str | None = None
    env_cache_override: dict[str, str] | None = None


def make_e2e_env_context(
    test_path: str,
    compose_project: str | None,
    env_cache_override: dict[str, str] | None,
) -> E2eEnvContext:
    return E2eEnvContext(
        test_path=test_path,
        compose_project=compose_project,
        env_cache_override=env_cache_override,
    )


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


def ensure_external_e2e_dependencies(playwright_root: Path) -> None:
    """Install dependencies for a TeaTree-managed external Playwright checkout.

    ``--repo`` clones live under TeaTree's cache, so the runner owns making them
    executable. User-provided ``T3_PRIVATE_TESTS`` directories remain
    user-managed and do not go through this helper.
    """
    package_json = playwright_root / "package.json"
    if not package_json.is_file():
        return
    node_modules = playwright_root / "node_modules"
    if node_modules.is_dir() and any(node_modules.iterdir()):
        return
    install_cmd = ["npm", "ci"] if (playwright_root / "package-lock.json").is_file() else ["npm", "install"]
    run_checked(install_cmd, cwd=playwright_root)


def resolve_private_tests_path() -> Path | None:
    """Resolve the private tests directory from the ``T3_PRIVATE_TESTS`` env or the DB config."""
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: keeps command import light

    private_tests = os.environ.get("T3_PRIVATE_TESTS", "") or cold_reader.str_setting("private_tests", default="")
    if not private_tests:
        return None
    path = Path(private_tests).expanduser()
    return path if path.is_dir() else None


def overlay_e2e_repo(e2e_config: Mapping[str, str]) -> E2ERepo | None:
    """Lift an overlay's ``get_e2e_config`` into an :class:`E2ERepo`, when it can.

    Returns an ``E2ERepo`` IFF the config carries BOTH a non-empty ``url`` and
    ``ref`` — the overlay declares its own E2E repo and the ref to source the
    suite from, so the ``external`` runner clones it by default (no ``--repo``,
    no ``T3_PRIVATE_TESTS``). The repo ``name`` is the last segment of
    ``project_path`` (falling back to ``"overlay-e2e"``); ``e2e_dir`` is the
    config's ``e2e_dir`` (default ``"e2e"``).

    Returns ``None`` otherwise (e.g. a trigger-ci-only config with a
    ``project_path`` + ``ref`` but no ``url``), so an overlay that supplies no
    ``url`` keeps the exact legacy ``T3_PRIVATE_TESTS`` behaviour.
    """
    url = e2e_config.get("url", "")
    ref = e2e_config.get("ref", "")
    if not url or not ref:
        return None
    name = e2e_config.get("project_path", "overlay-e2e").split("/")[-1] or "overlay-e2e"
    return E2ERepo(name=name, url=url, branch=ref, e2e_dir=e2e_config.get("e2e_dir", "e2e"))


def resolve_external_specs_path(repo: str, branch: str, *, overlay_repo: E2ERepo | None = None) -> Path:
    """Resolve the Playwright working directory for the ``external`` runner.

    Resolution order (first match wins):
    an explicit ``--repo <name>`` clones the configured ``[e2e_repos.<name>]`` at
    *branch* (or its default) and always wins;
    else, when *overlay_repo* is supplied (the overlay's
    :func:`overlay_e2e_repo`), it is cloned at its ``ref`` (a ``--branch``/``--ref``
    override wins so an open MR's branch can be run);
    else the ``T3_PRIVATE_TESTS`` directory is used. *branch* is only meaningful
    for a clone path — a ``T3_PRIVATE_TESTS`` directory is checked out by the user,
    so a branch there is a misuse.

    Raises :class:`E2eSpecsResolutionError` (carrying the CLI exit code) on any
    misconfiguration so the caller maps one exception to one ``SystemExit``.
    """
    if repo:
        repos_by_name = {r.name: r for r in load_e2e_repos()}
        if repo not in repos_by_name:
            raise E2eSpecsResolutionError.repo_not_in_config(repo)
        try:
            playwright_root = clone_or_update_e2e_repo(repos_by_name[repo], branch)
        except E2eBranchNotFoundError as exc:
            raise E2eSpecsResolutionError(str(exc), exit_code=1) from exc
        ensure_external_e2e_dependencies(playwright_root)
        return playwright_root
    if overlay_repo is not None:
        try:
            playwright_root = clone_or_update_e2e_repo(overlay_repo, branch)
        except E2eBranchNotFoundError as exc:
            raise E2eSpecsResolutionError(str(exc), exit_code=1) from exc
        ensure_external_e2e_dependencies(playwright_root)
        return playwright_root
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
    context: E2eEnvContext | None = None,
) -> dict[str, str]:
    """Build environment dict for Playwright: ``BASE_URL``, overlay extras, ``CI``.

    When *frontend_url* is given it overrides ``BASE_URL``.
    When it is ``None`` the existing ``BASE_URL`` env var is preserved (DEV / staging mode).

    *target* is the resolved dual-env target (``"dev"`` or ``"local"``); it is
    exported as ``T3_E2E_TARGET`` so a single dual-mode spec can branch on
    ``process.env.T3_E2E_TARGET === 'dev'`` instead of re-deriving the target
    from a ``BASE_URL`` host regex.

    *context.test_path* is the selected Playwright spec path. When present, it is
    threaded into the env cache visible to overlays as
    ``T3_E2E_TEST_PATH`` so overlay manifests can derive per-spec extras.

    *context.compose_project* is the teatree-managed docker-compose project
    of the resolved worktree (``compose_project(worktree)``) for a local
    target. It is exported as ``COMPOSE_PROJECT_NAME`` — the variable
    ``docker compose`` natively honours — so a spec that resolves the backend
    via a bare ``docker compose port web 8000`` / ``docker compose exec -T
    web`` (run from the backend repo dir, no ``-p``) deterministically
    targets the teatree stack whose ``web`` container has the
    restored-Postgres ``DATABASE_URL`` injected, instead of defaulting to the
    directory basename and missing it. ``None`` (dev target) leaves it unset.

    Overlay-specific env vars (e.g. ``CUSTOMER``) come from
    :meth:`OverlayE2E.env_extras` — core only knows about ``BASE_URL``,
    ``T3_E2E_TARGET``, ``COMPOSE_PROJECT_NAME``, ``T3_E2E_TEST_PATH`` and ``CI``.
    """
    env = {**os.environ}
    context = context or E2eEnvContext()
    if frontend_url is not None:
        env["BASE_URL"] = frontend_url
    env["T3_E2E_TARGET"] = target
    if context.compose_project:
        env["COMPOSE_PROJECT_NAME"] = context.compose_project

    if context.env_cache_override is not None:
        env_cache = context.env_cache_override
    else:
        envfile = _find_env_cache(_get_user_cwd())
        env_cache = _parse_env_file(envfile) if envfile is not None else {}
    if context.test_path:
        env_cache = {**env_cache, "T3_E2E_TEST_PATH": context.test_path}
    for key, value in get_overlay().e2e.env_extras(env_cache).items():
        env.setdefault(key, value)

    if headed:
        env.pop("CI", None)
    else:
        env["CI"] = "1"
    return env
