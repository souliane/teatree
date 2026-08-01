"""Dev-mode overlay install/uninstall for dogfooding teatree branches.

Supports the two layouts teatree is developed in, through one explicit concept —
the **teatree workspace**: the checkout the operator stands in that provides
teatree core.

**Standalone** (upstream) — teatree is its own repo and the workspace is a git
worktree of it, so ``root`` and ``core`` are the same directory. Overlays live in
separate repos, and installing one means making a sibling worktree of the
overlay's clone and editable-installing that into the teatree worktree's env.

**Vendored fork** — core is vendored at ``<fork>/vendor/teatree`` and edited in
place, so the git boundary is the FORK ROOT while the teatree ``pyproject.toml``
sits one level down. The overlay is co-located in that same repo (the fork
declares it in its own ``teatree.overlays`` entry points), so there is no sibling
checkout to make — the overlay's source IS the workspace's source.

Splitting ``root`` (the git boundary — where ``.git`` lives, where the branch and
the state file belong, and which env the editable install targets) from ``core``
(the directory holding teatree's ``pyproject.toml``) is what makes both layouts
one code path instead of a special case: the standalone layout is simply the case
where the two coincide.
"""

import importlib.util
import json
import tomllib
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path

import typer

from teatree.config import discover_overlays
from teatree.core.invocation_cwd import invocation_cwd
from teatree.utils.run import run_allowed_to_fail, run_checked

overlay_dev_app = typer.Typer(no_args_is_help=True, help="Dev-mode overlay install/uninstall.")


STATE_FILENAME = ".t3.local.json"

#: Where a fork vendors teatree core, relative to the fork root. Mirrors the
#: layout ``deploy/t3`` detects when it decides to bind-mount the fork root.
VENDORED_CORE_SUBPATH = Path("vendor") / "teatree"

OVERLAY_ENTRY_POINT_GROUP = "teatree.overlays"


class OverlayDevError(RuntimeError):
    """Raised when an overlay dev operation can't proceed."""


@dataclass(frozen=True)
class TeatreeWorkspace:
    """The checkout the operator stands in that provides teatree core.

    ``root`` is the git boundary; ``core`` is the directory carrying teatree's
    ``pyproject.toml``. They coincide in the standalone layout and differ by
    ``vendor/teatree`` in a fork.
    """

    root: Path
    core: Path

    @property
    def vendored(self) -> bool:
        return self.core != self.root

    @property
    def is_main_clone(self) -> bool:
        return (self.root / ".git").is_dir()


def _pyproject_data(pyproject: Path) -> dict:
    if not pyproject.is_file():
        return {}
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))


def _is_teatree_project(directory: Path) -> bool:
    return _pyproject_data(directory / "pyproject.toml").get("project", {}).get("name") == "teatree"


def _resolve_teatree_workspace(cwd: Path) -> TeatreeWorkspace:
    """Walk up from *cwd* to the teatree workspace, in either layout.

    A teatree source directory with no git boundary of its own is NOT an error on
    sight — that is exactly what vendored core looks like — so the walk remembers
    it and continues upward, only reporting it if no enclosing workspace turns up.
    That keeps the standalone layout's diagnostic intact while letting a fork
    resolve from inside ``vendor/teatree``.
    """
    orphan_core: Path | None = None
    for candidate in [cwd, *cwd.parents]:
        vendored_core = candidate / VENDORED_CORE_SUBPATH
        if _is_teatree_project(vendored_core) and (candidate / "pyproject.toml").is_file():
            if not (candidate / ".git").exists():
                msg = f"{candidate} has no .git marker"
                raise OverlayDevError(msg)
            return TeatreeWorkspace(root=candidate, core=vendored_core)

        if not (candidate / "pyproject.toml").is_file():
            continue
        if not _is_teatree_project(candidate):
            msg = f"{candidate} is not a teatree worktree"
            raise OverlayDevError(msg)

        git_marker = candidate / ".git"
        if git_marker.is_dir():
            msg = f"{candidate} is the main clone, not a worktree — refusing to install overlays"
            raise OverlayDevError(msg)
        if git_marker.is_file():
            return TeatreeWorkspace(root=candidate, core=candidate)
        orphan_core = orphan_core or candidate

    if orphan_core is not None:
        msg = f"{orphan_core} has no .git marker"
        raise OverlayDevError(msg)
    msg = f"No teatree workspace found walking up from {cwd}"
    raise OverlayDevError(msg)


def _workspace_declares_overlay(workspace: TeatreeWorkspace, name: str) -> bool:
    """Whether the workspace's own ``pyproject.toml`` provides *name* as an overlay.

    This is the co-location test, and it reads the tree the operator is standing
    in rather than the DB registry — deliberately. The registry stores ONE path
    that host and container interpret under different filesystem layouts, so it
    cannot answer "is this overlay part of the repo I am in?" on both sides. The
    entry-point declaration can, because it travels with the checkout.
    """
    entry_point_groups = _pyproject_data(workspace.root / "pyproject.toml").get("project", {}).get("entry-points", {})
    return name in entry_point_groups.get(OVERLAY_ENTRY_POINT_GROUP, {})


def _overlay_resolves_inside(workspace: TeatreeWorkspace, name: str) -> bool:
    """Whether the running environment already imports *name*'s package from this workspace.

    The command's goal is that the overlay resolves from the workspace's source.
    When it already does, an editable install would be a no-op at best — and in
    the container it is not even expressible, since the environment teatree runs
    in is a uv TOOL env while the workspace's ``.venv`` (a bind-mounted host one)
    belongs to the other side of the boundary. Asking where the module actually
    resolves answers precisely that, and stays honest on the host, where a fork
    WORKTREE legitimately needs the install because the module still resolves to
    the main clone.
    """
    for entry_point in entry_points(group=OVERLAY_ENTRY_POINT_GROUP):
        if entry_point.name != name:
            continue
        root_module = entry_point.value.split(":", 1)[0].split(".", 1)[0]
        try:
            spec = importlib.util.find_spec(root_module)
        except (ImportError, ValueError):
            return False
        if spec is None or not spec.origin:
            return False
        return Path(spec.origin).resolve().is_relative_to(workspace.root.resolve())
    return False


def _resolve_overlay_source(name: str) -> Path:
    """The overlay's own checkout, read through the boundary-aware discovery seam.

    Reading ``config.raw["overlays"][name]["path"]`` directly would take the stored
    value literally, and a ``~``-rooted registry path names nothing inside the
    container. ``discover_overlays`` already resolves that through
    ``_registry_project_path`` (falling back to where the package actually sits on
    this filesystem), so routing through it fixes the whole class here too.
    """
    for entry in discover_overlays():
        if entry.name != name:
            continue
        if entry.project_path is None:
            msg = f"Overlay {name!r} has no path configured in the DB overlays registry"
            raise OverlayDevError(msg)
        return entry.project_path.expanduser().resolve()
    msg = f"Overlay {name!r} not configured in the DB overlays registry"
    raise OverlayDevError(msg)


def _branch_exists(repo: Path, branch: str) -> bool:
    result = run_allowed_to_fail(
        ["git", "-C", str(repo), "rev-parse", "--verify", branch],
        expected_codes=None,
    )
    return result.returncode == 0


def _default_branch(repo: Path) -> str:
    result = run_allowed_to_fail(
        ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
        expected_codes=None,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def _current_branch(worktree: Path) -> str:
    result = run_allowed_to_fail(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        expected_codes=None,
    )
    return result.stdout.strip() or "main"


def _ensure_sibling_worktree(workspace: TeatreeWorkspace, main_clone: Path, *, branch: str) -> Path:
    """A worktree of the overlay's clone beside the teatree workspace.

    The main-clone refusal lives HERE, at the hazard it actually guards: this is
    the step that would ``git worktree add`` into ``<parent>/<overlay-repo>``,
    which beside a main clone is the overlay's OWN main clone. A vendored fork
    whose overlay is co-located never reaches this function, which is why that
    layout can legitimately run from its main clone — it creates no sibling.
    """
    if workspace.is_main_clone:
        msg = f"{workspace.root} is the main clone, not a worktree — refusing to install overlays"
        raise OverlayDevError(msg)
    sibling = workspace.root.parent / main_clone.name
    if sibling.exists():
        return sibling
    target_branch = branch if _branch_exists(main_clone, branch) else _default_branch(main_clone)
    run_checked(["git", "-C", str(main_clone), "worktree", "add", str(sibling), target_branch])
    return sibling


def _uv_pip_install_editable(workspace_root: Path, overlay_path: Path) -> None:
    # `--no-deps` precedes `--editable`: `--editable` takes a VALUE, so
    # `--editable --no-deps <path>` makes uv consume the flag as that value and
    # refuse with "a value is required for '--editable <EDITABLE>'".
    run_checked(
        ["uv", "pip", "install", "--no-deps", "--editable", str(overlay_path)],
        cwd=workspace_root,
    )


def _uv_pip_uninstall(workspace_root: Path, name: str) -> None:
    run_allowed_to_fail(
        ["uv", "pip", "uninstall", name],
        cwd=workspace_root,
        expected_codes=None,
    )


def _load_state(workspace_root: Path) -> dict:
    path = workspace_root / STATE_FILENAME
    if not path.is_file():
        return {"overlays": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(workspace_root: Path, state: dict) -> None:
    (workspace_root / STATE_FILENAME).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


@overlay_dev_app.command("install")
def install(name: str = typer.Argument(..., help="Overlay name as configured in the DB overlays registry.")) -> None:
    """Install an overlay editable into the current teatree workspace for dogfooding."""
    try:
        workspace = _resolve_teatree_workspace(invocation_cwd())
        if _workspace_declares_overlay(workspace, name):
            source = workspace.root
            already_provided = _overlay_resolves_inside(workspace, name)
        else:
            main_clone = _resolve_overlay_source(name)
            branch = _current_branch(workspace.root)
            source = _ensure_sibling_worktree(workspace, main_clone, branch=branch)
            already_provided = False
        if not already_provided:
            _uv_pip_install_editable(workspace.root, source)
    except OverlayDevError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    state = _load_state(workspace.root)
    state.setdefault("overlays", {})[name] = {"source": str(source)}
    _save_state(workspace.root, state)
    if already_provided:
        typer.echo(f"{name} is already provided by this workspace ({source}) — nothing to install")
    else:
        typer.echo(f"Installed {name} from {source}")


@overlay_dev_app.command("uninstall")
def uninstall(name: str = typer.Argument(..., help="Overlay name to uninstall.")) -> None:
    """Uninstall an overlay from the current teatree workspace venv."""
    try:
        workspace = _resolve_teatree_workspace(invocation_cwd())
    except OverlayDevError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    _uv_pip_uninstall(workspace.root, name)
    state = _load_state(workspace.root)
    state.setdefault("overlays", {}).pop(name, None)
    _save_state(workspace.root, state)
    typer.echo(f"Uninstalled {name}")


@overlay_dev_app.command("status")
def status() -> None:
    """Show overlays currently installed into this teatree workspace."""
    try:
        workspace = _resolve_teatree_workspace(invocation_cwd())
    except OverlayDevError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    overlays = _load_state(workspace.root).get("overlays", {})
    if not overlays:
        typer.echo("No overlays installed in this worktree.")
        return
    for overlay_name, info in sorted(overlays.items()):
        typer.echo(f"  {overlay_name}  <-  {info.get('source', '?')}")
