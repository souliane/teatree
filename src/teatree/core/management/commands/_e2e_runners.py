"""External/private-test repo resolution and Playwright env construction.

Split out of ``e2e.py`` (mirroring the ``_e2e_discovery`` and
``_test_plan`` splits) to keep that module under the project's per-file
LOC cap. These are the pure helpers the ``external``/``project`` runners
lean on: cloning the external test repo, resolving the private-tests
directory, and building the Playwright environment dict.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import typer

from teatree.config import E2ERepo, load_e2e_repos
from teatree.core.e2e_scenario import E2eExtrasContext
from teatree.core.intake.resolve import _find_env_cache, _get_user_cwd, _parse_env_file, resolve_worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.worktree.worktree_env import CACHE_DIRNAME
from teatree.paths import get_data_dir
from teatree.utils.run import CommandFailedError, run_checked, run_streamed

#: The out-of-repo capture directory the runner exports as
#: ``T3_E2E_ARTIFACTS_DIR`` (#3331): ``<ticket_dir>/.t3-cache/artifacts`` — a
#: sibling of every repo working tree, never inside one. The env var whose value
#: satisfies the "no artifacts inside a repo" rule core mandates, so the rule is
#: structural (the runner sets it) rather than advisory (each overlay re-derives it).
ARTIFACTS_ENV = "T3_E2E_ARTIFACTS_DIR"
_ARTIFACTS_SUBDIR = "artifacts"

#: The evidence-capture flag the runner exports on every managed run (#3331). A
#: managed run through the runner captures evidence; a plain ``npx playwright`` /
#: CI run leaves it unset — parity comes from omission, not from each overlay
#: remembering to inject it.
CAPTURE_EVIDENCE_ENV = "T3_E2E_CAPTURE_EVIDENCE"


class ArtifactsDirInRepoError(RuntimeError):
    """An explicit ``--artifacts-dir`` resolves inside a repo working tree.

    Refused (#3331): captures written under a repo put binaries in a source tree
    (#3091, the mistake the no-artifacts-in-a-repo rule already forbids), so an
    explicitly-passed dir that sits inside any git working tree is a hard error.
    """

    def __init__(self, artifacts_dir: Path, repo_root: Path) -> None:
        super().__init__(
            f"--artifacts-dir {artifacts_dir} is inside the repo working tree {repo_root} "
            "(a '.git' lives there). E2E artifacts must live outside every repo working tree — "
            "pass a path under the out-of-repo .t3-cache/artifacts root, or omit --artifacts-dir "
            "to let the runner derive it.",
        )


def e2e_artifacts_root(worktree_path: str) -> Path:
    """Derive the out-of-repo artifacts root for a resolved worktree path.

    ``<ticket_dir>/.t3-cache/artifacts`` — ``ticket_dir`` is the parent holding
    the ticket's sibling repos, so the root is out of every repo working tree.
    """
    return Path(worktree_path).parent / CACHE_DIRNAME / _ARTIFACTS_SUBDIR


def refuse_artifacts_dir_in_repo(artifacts_dir: Path) -> None:
    """Raise :class:`ArtifactsDirInRepoError` when *artifacts_dir* sits inside a git working tree."""
    resolved = artifacts_dir.expanduser()
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            raise ArtifactsDirInRepoError(resolved, ancestor)


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
    artifacts_dir: str = ""
    capture_evidence: bool = True


def make_e2e_env_context(
    test_path: str,
    compose_project: str | None,
    env_cache_override: dict[str, str] | None,
    *,
    artifacts_dir: str = "",
    capture_evidence: bool = True,
) -> E2eEnvContext:
    return E2eEnvContext(
        test_path=test_path,
        compose_project=compose_project,
        env_cache_override=env_cache_override,
        artifacts_dir=artifacts_dir,
        capture_evidence=capture_evidence,
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

    *context.artifacts_dir* is the out-of-repo capture root the runner resolved;
    it is exported as ``T3_E2E_ARTIFACTS_DIR`` so a capture lands outside every
    working tree without the overlay re-deriving the path. *context.capture_evidence*
    exports ``T3_E2E_CAPTURE_EVIDENCE`` on a managed run (opt out with
    ``--no-evidence``); a plain / CI run leaves it unset.

    The resolved target, spec path, artifacts dir and compose project are handed
    to :meth:`OverlayE2E.env_extras` as a frozen :class:`E2eExtrasContext`, so an
    overlay's extras key off the *same* target core routed at — never a re-derived
    ``BASE_URL`` host regex. Overlay-specific env vars (e.g. ``CUSTOMER``) come
    from that seam — core only knows about ``BASE_URL``, ``T3_E2E_TARGET``,
    ``COMPOSE_PROJECT_NAME``, ``T3_E2E_TEST_PATH``, ``T3_E2E_ARTIFACTS_DIR``,
    ``T3_E2E_CAPTURE_EVIDENCE`` and ``CI``.
    """
    env = {**os.environ}
    context = context or E2eEnvContext()
    if frontend_url is not None:
        env["BASE_URL"] = frontend_url
    env["T3_E2E_TARGET"] = target
    if context.compose_project:
        env["COMPOSE_PROJECT_NAME"] = context.compose_project
    if context.artifacts_dir:
        env[ARTIFACTS_ENV] = context.artifacts_dir
    if context.capture_evidence:
        env[CAPTURE_EVIDENCE_ENV] = "1"

    if context.env_cache_override is not None:
        env_cache = context.env_cache_override
    else:
        envfile = _find_env_cache(_get_user_cwd())
        env_cache = _parse_env_file(envfile) if envfile is not None else {}
    if context.test_path:
        env_cache = {**env_cache, "T3_E2E_TEST_PATH": context.test_path}
    extras_context = E2eExtrasContext(
        target=target,
        spec_path=context.test_path,
        artifacts_dir=context.artifacts_dir,
        compose_project=context.compose_project or "",
    )
    for key, value in get_overlay().e2e.env_extras(env_cache, context=extras_context).items():
        env.setdefault(key, value)

    if headed:
        env.pop("CI", None)
    else:
        env["CI"] = "1"
    return env


@dataclass(frozen=True)
class ProjectRunOptions:
    """Flags for the in-repo ``project`` runner, resolved by the command."""

    test_path: str = ""
    resolved_target: str = ""
    headed: bool = False
    docker: bool = True
    update_snapshots: bool = False
    artifacts_dir: str = ""
    capture_evidence: bool = True


def _project_worktree_path() -> str:
    """The resolved worktree path for the in-repo runner, or ``"."`` when unresolved."""
    try:
        worktree = resolve_worktree()
    except Exception:  # noqa: BLE001 — an unresolvable worktree degrades to cwd, never aborts the run
        return "."
    return (worktree.extra or {}).get("worktree_path", ".") if worktree else "."


def _managed_run_env(opts: ProjectRunOptions, settings_module: str) -> dict[str, str]:
    """Env for the in-process pytest-playwright run: settings, target, artifacts, evidence, ``CI``."""
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module, "T3_E2E_TARGET": opts.resolved_target}
    if opts.artifacts_dir:
        env[ARTIFACTS_ENV] = opts.artifacts_dir
    if opts.capture_evidence:
        env[CAPTURE_EVIDENCE_ENV] = "1"
    if opts.headed:
        env.pop("CI", None)
    else:
        env["CI"] = "1"
    return env


def _docker_managed_env_flags(opts: ProjectRunOptions) -> list[str]:
    """``-e KEY=VALUE`` flags carrying the managed-run vars into the compose ``e2e`` service."""
    flags = ["-e", f"T3_E2E_TARGET={opts.resolved_target}"]
    if opts.artifacts_dir:
        flags.extend(["-e", f"{ARTIFACTS_ENV}={opts.artifacts_dir}"])
    if opts.capture_evidence:
        flags.extend(["-e", f"{CAPTURE_EVIDENCE_ENV}=1"])
    return flags


def run_project_suite(opts: ProjectRunOptions, *, write_err: Callable[[str], None]) -> str:
    """Run the project's own e2e suite (in-repo pytest-playwright or the compose ``e2e`` service).

    The runner owns the managed-run env: it exports ``T3_E2E_TARGET``, the
    out-of-repo ``T3_E2E_ARTIFACTS_DIR``, and the ``T3_E2E_CAPTURE_EVIDENCE``
    flag (#3331). Returns ``"E2E passed."`` on green; raises ``SystemExit`` with
    the Playwright/pytest exit code on red.
    """
    wt_path = _project_worktree_path()
    e2e_config = get_overlay().metadata.get_e2e_config()
    settings_module = e2e_config.get("settings_module", "e2e.settings")
    test_dir = opts.test_path or e2e_config.get("test_dir", "e2e/")

    if opts.docker and not Path("/.dockerenv").exists():
        compose_file = Path(wt_path) / "dev" / "docker-compose.yml"
        if compose_file.is_file():
            cmd = ["docker", "compose", "-f", str(compose_file), "run", "--rm"]
            cmd.extend(_docker_managed_env_flags(opts))
            cmd.extend(["e2e", test_dir])
            if opts.update_snapshots:
                cmd.append("--update-snapshots")
            rc = run_streamed(cmd, cwd=wt_path, check=False)
            if rc == 0:
                return "E2E passed."
            write_err(f"E2E failed (exit {rc}).")
            raise SystemExit(rc)

    cmd = ["uv", "run", "pytest", test_dir]
    cmd.extend(["-o", f"DJANGO_SETTINGS_MODULE={settings_module}", "--no-cov", "-p", "no:tach", "-v"])
    if opts.update_snapshots:
        cmd.append("--update-snapshots")
    rc = run_streamed(cmd, cwd=wt_path, env=_managed_run_env(opts, settings_module), check=False)
    if rc == 0:
        return "E2E passed."
    write_err(f"E2E failed (exit {rc}).")
    raise SystemExit(rc)
