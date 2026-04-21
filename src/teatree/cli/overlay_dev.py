"""Dev-mode overlay install/uninstall for dogfooding teatree branches."""

import json
import tomllib
from pathlib import Path

import typer

from teatree.config import CONFIG_PATH, load_config
from teatree.utils.run import run_allowed_to_fail, run_checked

overlay_dev_app = typer.Typer(no_args_is_help=True, help="Dev-mode overlay install/uninstall.")


STATE_FILENAME = ".t3.local.json"


class OverlayDevError(RuntimeError):
    """Raised when an overlay dev operation can't proceed."""


def _resolve_teatree_worktree(cwd: Path) -> Path:
    for candidate in [cwd, *cwd.parents]:
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        if data.get("project", {}).get("name") != "teatree":
            msg = f"{candidate} is not a teatree worktree"
            raise OverlayDevError(msg)
        git_marker = candidate / ".git"
        if git_marker.is_dir():
            msg = f"{candidate} is the main clone, not a worktree — refusing to install overlays"
            raise OverlayDevError(msg)
        if not git_marker.is_file():
            msg = f"{candidate} has no .git marker"
            raise OverlayDevError(msg)
        return candidate
    msg = f"No teatree worktree found walking up from {cwd}"
    raise OverlayDevError(msg)


def _resolve_overlay_source(name: str, config_path: Path | None = None) -> Path:
    effective_path = config_path if config_path is not None else CONFIG_PATH
    config = load_config(effective_path)
    overlay_cfg = config.raw.get("overlays", {}).get(name)
    if not overlay_cfg:
        msg = f"Overlay {name!r} not configured in {effective_path}"
        raise OverlayDevError(msg)
    path = overlay_cfg.get("path", "")
    if not path:
        msg = f"Overlay {name!r} has no path configured in {effective_path}"
        raise OverlayDevError(msg)
    return Path(path).expanduser().resolve()


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


def _ensure_sibling_worktree(teatree_worktree: Path, main_clone: Path, *, branch: str) -> Path:
    sibling = teatree_worktree.parent / main_clone.name
    if sibling.exists():
        return sibling
    target_branch = branch if _branch_exists(main_clone, branch) else _default_branch(main_clone)
    run_checked(["git", "-C", str(main_clone), "worktree", "add", str(sibling), target_branch])
    return sibling


def _uv_pip_install_editable(teatree_worktree: Path, overlay_path: Path) -> None:
    run_checked(
        ["uv", "pip", "install", "--editable", "--no-deps", str(overlay_path)],
        cwd=teatree_worktree,
    )


def _uv_pip_uninstall(teatree_worktree: Path, name: str) -> None:
    run_allowed_to_fail(
        ["uv", "pip", "uninstall", name],
        cwd=teatree_worktree,
        expected_codes=None,
    )


def _load_state(worktree: Path) -> dict:
    path = worktree / STATE_FILENAME
    if not path.is_file():
        return {"overlays": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(worktree: Path, state: dict) -> None:
    (worktree / STATE_FILENAME).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


@overlay_dev_app.command("install")
def install(name: str = typer.Argument(..., help="Overlay name as configured in ~/.teatree.toml.")) -> None:
    """Install an overlay editable into the current teatree worktree for dogfooding."""
    try:
        worktree = _resolve_teatree_worktree(Path.cwd())
        main_clone = _resolve_overlay_source(name)
        branch = _current_branch(worktree)
        sibling = _ensure_sibling_worktree(worktree, main_clone, branch=branch)
        _uv_pip_install_editable(worktree, sibling)
    except OverlayDevError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    state = _load_state(worktree)
    state.setdefault("overlays", {})[name] = {"source": str(sibling)}
    _save_state(worktree, state)
    typer.echo(f"Installed {name} from {sibling}")


@overlay_dev_app.command("uninstall")
def uninstall(name: str = typer.Argument(..., help="Overlay name to uninstall.")) -> None:
    """Uninstall an overlay from the current teatree worktree venv."""
    try:
        worktree = _resolve_teatree_worktree(Path.cwd())
    except OverlayDevError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    _uv_pip_uninstall(worktree, name)
    state = _load_state(worktree)
    state.setdefault("overlays", {}).pop(name, None)
    _save_state(worktree, state)
    typer.echo(f"Uninstalled {name}")


@overlay_dev_app.command("status")
def status() -> None:
    """Show overlays currently installed into this teatree worktree."""
    try:
        worktree = _resolve_teatree_worktree(Path.cwd())
    except OverlayDevError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    overlays = _load_state(worktree).get("overlays", {})
    if not overlays:
        typer.echo("No overlays installed in this worktree.")
        return
    for overlay_name, info in sorted(overlays.items()):
        typer.echo(f"  {overlay_name}  <-  {info.get('source', '?')}")
