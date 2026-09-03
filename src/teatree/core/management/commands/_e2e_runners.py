"""External specs repo resolution and Playwright env construction.

Split out of ``e2e.py`` (mirroring the ``_e2e_discovery`` and
``_test_plan`` splits) to keep that module under the project's per-file
LOC cap. These are the pure helpers the ``external``/``project`` runners
lean on: cloning the external specs repo and building the Playwright
environment dict.
"""

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import typer

import teatree.core.management.commands._e2e_specs_checkout as _specs
from teatree.config import E2ERepo, load_e2e_repos
from teatree.core.e2e_scenario import E2eExtrasContext
from teatree.core.intake.resolve import _find_env_cache, _get_user_cwd, _parse_env_file, resolve_worktree
from teatree.core.management.commands._e2e_specs_checkout import SpecsCheckoutBusyError
from teatree.core.overlay_loader import get_overlay
from teatree.core.worktree.worktree_env import CACHE_DIRNAME
from teatree.paths import get_data_dir
from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked, run_streamed

__all__ = ["SpecsCheckoutBusyError"]

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

#: The explicit-scheme ssh remote spelling, alongside the scp-like ``git@host:path``.
_SSH_SCHEME = "ssh://"


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
    extra: list[str] = field(default_factory=list)

    def to_args(self) -> list[str]:
        args: list[str] = []
        if self.test_path:
            args.append(self.test_path)
        args.append("--reporter=list")
        if self.update_snapshots:
            args.append("--update-snapshots")
        args.extend(self.extra)
        return args


class E2eSpecsRemoteError(RuntimeError):
    """A specs clone failed for a reason the remote itself explains."""

    def __init__(self, message: str, *, ref: str) -> None:
        super().__init__(message)
        self.ref = ref


class E2eBranchNotFoundError(E2eSpecsRemoteError):
    """The requested E2E specs ref does not exist on the remote."""

    def __init__(self, *, name: str, ref: str, url: str) -> None:
        super().__init__(
            f"E2E specs branch '{ref}' not found on repo '{name}' ({url}). "
            "Pass an existing --branch/--ref, or check the open MR's source branch name.",
            ref=ref,
        )


class E2eSpecsRemoteUnreachableError(E2eSpecsRemoteError):
    """The specs remote refused or could not be contacted, so ref existence is UNKNOWN.

    Never conflate this with :class:`E2eBranchNotFoundError`: a failed ``git
    ls-remote`` prints nothing on stdout, exactly like a successful listing of an
    absent ref, so the two are indistinguishable without the exit code — and
    reporting an auth failure as "branch not found" sends the reader hunting a ref
    the remote demonstrably has.
    """

    def __init__(self, *, name: str, ref: str, url: str, detail: str) -> None:
        super().__init__(
            f"E2E specs remote '{name}' ({url}) could not be reached: git AUTHENTICATION or "
            f"network failure, so whether '{ref}' exists there is UNKNOWN — this is NOT a "
            "missing branch. TeaTree authenticates an https remote with GITLAB_TOKEN through "
            "git's credential helper; confirm that variable is set and valid in THIS venue "
            f"(a container does not inherit the host's login). git said:\n{detail}",
            ref=ref,
        )


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
    def no_specs_source(cls) -> "E2eSpecsResolutionError":
        msg = (
            "No E2E specs source: this overlay declares no e2e repo (a `url` + `ref` from "
            "get_e2e_config), so pass an explicit --repo <name> from the e2e_repos config."
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

    The checkout is keyed by repo AND ref, so a run never resets a tree another
    run is executing against; :func:`_specs.hold_for_process`, claimed by
    :func:`resolve_external_specs_path` before this runs, serialises the residual
    case of two runs on the SAME ref.

    Raises :class:`E2eBranchNotFoundError` when the ref does not exist on the
    remote, so a typo'd or stale branch fails with a clear message rather than
    an opaque git error.

    Returns ``cache_path / repo.e2e_dir`` — the directory passed as ``cwd`` to Playwright.
    """
    ref = branch_override or repo.branch
    url = _fetchable_url(repo.url)
    specs_root = get_data_dir(_specs.SPECS_NAMESPACE)
    cache_path = _specs.checkout_path(specs_root, repo.name, ref)
    _specs.prune_stale_checkouts(specs_root, repo.name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not cache_path.exists():
            run_checked(["git", "clone", "--branch", ref, "--depth", "1", url, str(cache_path)])
        else:
            # Fetch by URL rather than `origin`: a cache cloned before this resolution
            # existed still carries the ssh-form remote, which is unreachable here.
            run_checked(["git", "-C", str(cache_path), "fetch", url, ref])
            run_checked(["git", "-C", str(cache_path), "reset", "--hard", "FETCH_HEAD"])
    except CommandFailedError as exc:
        state, detail = _remote_ref_state(url, ref)
        if state is _RefState.UNREACHABLE:
            raise E2eSpecsRemoteUnreachableError(name=repo.name, ref=ref, url=repo.url, detail=detail) from exc
        if state is _RefState.ABSENT:
            raise E2eBranchNotFoundError(name=repo.name, ref=ref, url=repo.url) from exc
        raise
    return cache_path / repo.e2e_dir


def _fetchable_url(url: str) -> str:
    """The configured URL, or its HTTPS form on a host that cannot authenticate over ssh.

    A container venue runs git with an HTTPS credential helper (deploy/Dockerfile,
    ``git config --system``) fed by ``GITLAB_TOKEN``, and carries no key material at
    all. An ssh-form remote consults neither, so git dies on "Host key verification
    failed" while the same repo is one Authorization header away over HTTPS. The
    predicate is the IDENTITY, never the binary: the image ships ``ssh`` as a
    transitive package dependency and still has no ``~/.ssh``, which is exactly the
    venue this rewrite exists for. A host holding a key keeps its configured remote,
    so a developer laptop and CI behave as before.
    """
    host, path = _split_ssh_url(url)
    if host is None or _ssh_identity_available():
        return url
    return f"https://{host}/{path}"


def _split_ssh_url(url: str) -> tuple[str | None, str]:
    """``(host, path)`` for either ssh spelling, ``(None, "")`` for any other scheme."""
    if url.startswith("git@"):
        host, _, path = url.partition(":")
        return host.removeprefix("git@"), path
    if url.startswith(_SSH_SCHEME):
        authority, _, path = url.removeprefix(_SSH_SCHEME).partition("/")
        return authority.rpartition("@")[2], path
    return None, ""


def _ssh_identity_available() -> bool:
    """Whether this host can actually authenticate over ssh — a client AND an identity.

    ``shutil.which("ssh")`` alone answers the wrong question, which is how a venue
    with a binary and no key kept an unusable remote.
    """
    if not shutil.which("ssh"):
        return False
    if os.environ.get("SSH_AUTH_SOCK"):
        return True
    ssh_dir = Path.home() / ".ssh"
    return any(ssh_dir.glob("id_*")) or (ssh_dir / "config").is_file()


class _RefState(Enum):
    PRESENT = auto()
    ABSENT = auto()
    UNREACHABLE = auto()


def _remote_ref_state(url: str, ref: str) -> tuple[_RefState, str]:
    """Whether *ref* is on *url*, plus git's stderr when the remote could not be read.

    The exit code is the whole point: a refused ``ls-remote`` and a successful one
    listing an absent ref both print nothing, so stdout alone cannot tell an auth
    failure from a missing branch and reports every one of them as the latter.
    """
    listing = run_allowed_to_fail(["git", "ls-remote", "--heads", "--tags", url, ref], expected_codes=None)
    if listing.returncode != 0:
        return _RefState.UNREACHABLE, "\n".join((listing.stderr or "").strip().splitlines()[-5:])
    return (_RefState.PRESENT if listing.stdout.strip() else _RefState.ABSENT), ""


def ensure_external_e2e_dependencies(playwright_root: Path) -> None:
    """Install dependencies for a TeaTree-managed external Playwright checkout.

    Every resolved specs source is a clone under TeaTree's cache, so the runner
    owns making them executable.
    """
    package_json = playwright_root / "package.json"
    if not package_json.is_file():
        return
    _ensure_node_modules(playwright_root)
    _ensure_playwright_browsers(playwright_root)


def _ensure_node_modules(playwright_root: Path) -> None:
    node_modules = playwright_root / "node_modules"
    if node_modules.is_dir() and any(node_modules.iterdir()):
        return
    install_cmd = ["npm", "ci"] if (playwright_root / "package-lock.json").is_file() else ["npm", "install"]
    run_checked(install_cmd, cwd=playwright_root)


def _playwright_browsers_dir() -> Path:
    """Where Playwright keeps its downloaded browsers."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _ensure_playwright_browsers(playwright_root: Path) -> None:
    """Install the browser THIS clone pins, checked independently of node deps.

    Node modules and browsers are separately satisfiable, so they get separate
    guards. Folding them together -- returning early on a populated
    ``node_modules`` -- made this a no-op on precisely the state a repeat run has,
    so a clone whose browsers were never downloaded could never acquire them and
    every run died in ``browserType.launch``.

    Driven from the clone rather than baked into the image on purpose: the browser
    build is pinned by the clone's own ``@playwright/test``, so an image-level
    browser silently drifts the next time either side moves.
    """
    if not (playwright_root / "node_modules" / "@playwright" / "test").is_dir():
        return
    browsers = _playwright_browsers_dir()
    if browsers.is_dir() and any(browsers.glob("chromium*")):
        return
    run_checked(["npx", "playwright", "install", "chromium"], cwd=playwright_root)


def overlay_e2e_repo(e2e_config: Mapping[str, str]) -> E2ERepo | None:
    """Lift an overlay's ``get_e2e_config`` into an :class:`E2ERepo`, when it can.

    Returns an ``E2ERepo`` IFF the config carries BOTH a non-empty ``url`` and
    ``ref`` — the overlay declares its own E2E repo and the ref to source the
    suite from, so the ``external`` runner clones it by default (no ``--repo``).
    The repo ``name`` is the last segment of ``project_path`` (falling back to
    ``"overlay-e2e"``); ``e2e_dir`` is the config's ``e2e_dir`` (default ``"e2e"``).

    Returns ``None`` otherwise (e.g. a trigger-ci-only config with a
    ``project_path`` + ``ref`` but no ``url``), leaving ``--repo`` as the only
    way to name a specs source for that overlay.
    """
    url = e2e_config.get("url", "")
    ref = e2e_config.get("ref", "")
    if not url or not ref:
        return None
    name = e2e_config.get("project_path", "overlay-e2e").split("/")[-1] or "overlay-e2e"
    return E2ERepo(name=name, url=url, branch=ref, e2e_dir=e2e_config.get("e2e_dir", "e2e"))


def resolve_external_specs_path(repo: str, branch: str, *, overlay_repo: E2ERepo | None = None) -> Path:
    """Resolve the Playwright working directory for the ``external`` runner.

    Specs live in the repo that owns them, so every source is a declared repo
    cloned at a ref. Resolution order (first match wins) — EXPLICIT beats DEFAULT:
    an explicit ``--repo <name>`` clones the configured ``[e2e_repos.<name>]`` at
    *branch* (or its default) and always wins; else *overlay_repo* (the overlay's
    :func:`overlay_e2e_repo`, its declared ``url`` + ``ref``) is cloned, with a
    ``--branch``/``--ref`` override winning so an open MR's branch can be run.

    Raises :class:`E2eSpecsResolutionError` (carrying the CLI exit code) on any
    misconfiguration, and :class:`SpecsCheckoutBusyError` when a live run already
    holds this repo+ref checkout. The caller maps each to a ``SystemExit``.
    """
    specs_repo = resolve_specs_repo(repo, overlay_repo=overlay_repo)
    # Claimed for the whole process, not just this call: the caller hands the returned
    # path to Playwright and runs for minutes, which is precisely the window a rival
    # run's `reset --hard` would land in.
    _specs.hold_for_process(
        get_data_dir(_specs.SPECS_NAMESPACE),
        specs_repo.name,
        branch or specs_repo.branch,
    )
    return _clone_specs_repo(specs_repo, branch)


def resolve_specs_repo(repo: str, *, overlay_repo: E2ERepo | None = None) -> E2ERepo:
    """Which :class:`E2ERepo` the ``external`` runner sources specs from.

    Split out of :func:`resolve_external_specs_path` because the process-lifetime
    claim is keyed by the repo's name and ref, so the caller has to know WHICH
    repo before anything touches the disk.
    """
    if repo:
        repos_by_name = {r.name: r for r in load_e2e_repos()}
        if repo not in repos_by_name:
            raise E2eSpecsResolutionError.repo_not_in_config(repo)
        return repos_by_name[repo]
    if overlay_repo is None:
        raise E2eSpecsResolutionError.no_specs_source()
    return overlay_repo


def _clone_specs_repo(specs_repo: E2ERepo, branch: str) -> Path:
    try:
        playwright_root = clone_or_update_e2e_repo(specs_repo, branch)
    except E2eSpecsRemoteError as exc:
        raise E2eSpecsResolutionError(str(exc), exit_code=1) from exc
    ensure_external_e2e_dependencies(playwright_root)
    return playwright_root


def build_e2e_env(
    frontend_url: str | None = None,
    *,
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

    The resolved target, spec path, artifacts dir, compose project and base URL
    are handed to :meth:`OverlayE2E.env_extras` as a frozen :class:`E2eExtrasContext`,
    so an overlay's extras key off the *same* values core resolved for the child
    process — never a re-derivation from ``os.environ``, which still holds the
    host process's env at the point ``env_extras`` runs (the constructed *env*
    dict above is local until the subprocess is spawned). Overlay-specific env
    vars (e.g. ``CUSTOMER``) come from that seam — core only knows about
    ``BASE_URL``, ``T3_E2E_TARGET``, ``COMPOSE_PROJECT_NAME``, ``T3_E2E_TEST_PATH``,
    ``T3_E2E_ARTIFACTS_DIR``, ``T3_E2E_CAPTURE_EVIDENCE`` and ``CI``.
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
        base_url=env.get("BASE_URL", ""),
    )
    for key, value in get_overlay().e2e.env_extras(env_cache, context=extras_context).items():
        env.setdefault(key, value)

    env["CI"] = "1"
    return env


@dataclass(frozen=True)
class ProjectRunOptions:
    """Flags for the in-repo ``project`` runner, resolved by the command."""

    test_path: str = ""
    resolved_target: str = ""
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
