"""TeaTree CLI — single ``t3`` entry point for all commands.

DB-touching commands are django-typer management commands, exposed here after
``django.setup()``.  Django-free commands live as plain Typer groups.

Each command (or small related cluster) lives in its own ``cli/<name>.py``
module.  This file holds only the Typer app construction, root callback,
sub-app registration, ``main`` entry point, and the project-root helpers
shared across modules.
"""

import logging
from pathlib import Path

import typer

import teatree.cli.agent as _agent
import teatree.cli.info as _info
import teatree.cli.sessions as _sessions
from teatree.cli.assess import assess_app
from teatree.cli.ci import ci_app
from teatree.cli.config import config_app
from teatree.cli.doctor import DoctorService, IntrospectionHelpers, doctor_app
from teatree.cli.infra import infra_app
from teatree.cli.loop import loop_app
from teatree.cli.overlay import OverlayAppBuilder
from teatree.cli.overlay_dev import overlay_dev_app
from teatree.cli.review import review_app
from teatree.cli.review_request import review_request_app
from teatree.cli.setup import setup_app
from teatree.cli.tools import tool_app

logger = logging.getLogger(__name__)

__all__ = ["app", "main"]

app = typer.Typer(name="t3", no_args_is_help=True, add_completion=False)


@app.callback()
def _root_callback(ctx: typer.Context) -> None:
    ctx.ensure_object(dict)
    _maybe_show_update_notice()


def _maybe_show_update_notice() -> None:
    """Show update notice at most once per day, if enabled in user settings."""
    try:
        from teatree.config import check_for_updates  # noqa: PLC0415

        message = check_for_updates()
        if message:
            typer.echo(f"[update] {message}", err=True)
    except Exception:  # noqa: BLE001, S110
        pass


def _find_project_root() -> Path:
    """Walk up from cwd to find the project root (contains pyproject.toml)."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        if (directory / "pyproject.toml").is_file():
            return directory
    return Path.cwd()


def _find_overlay_project() -> Path:
    """Find the active overlay project root."""
    from teatree.config import discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    if active and active.project_path:
        return active.project_path
    return _find_project_root()


# ── Command registration (preserves original help-output order) ───────

app.command()(_info.startoverlay)
app.command()(_info.docs)
app.command()(_agent.agent)
app.command()(_sessions.sessions)
app.command()(_info.info)
app.add_typer(config_app, name="config")
app.add_typer(ci_app, name="ci")
app.add_typer(review_app, name="review")
app.add_typer(review_request_app, name="review-request")
app.add_typer(doctor_app, name="doctor")
app.add_typer(tool_app, name="tool")
app.add_typer(setup_app, name="setup")
app.add_typer(assess_app, name="assess")
app.add_typer(overlay_dev_app, name="overlay")
app.add_typer(infra_app, name="infra")
app.add_typer(loop_app, name="loop")


# ── Django-dependent overlay command groups ───────────────────────────


def register_overlay_commands(allowlist: set[str] | None = None) -> None:
    """Register all installed overlays as subcommand groups.

    No Django bootstrap needed — commands delegate to manage.py via subprocess.
    Pass *allowlist* of entry names (e.g. ``{"t3-teatree"}``) to register a subset —
    used by the CLI reference generator to keep generated docs deterministic.
    """
    from teatree.config import discover_active_overlay, discover_overlays  # noqa: PLC0415

    active = discover_active_overlay()
    installed = discover_overlays()

    for entry in installed:
        if allowlist is not None and entry.name not in allowlist:
            continue
        short_name = entry.name.removeprefix("t3-")
        project_path = entry.project_path or (active.project_path if active and active.name == entry.name else None)
        # Entry-point overlays use teatree base settings; TOML overlays with their own
        # project dir may have a settings module stored in overlay_class as fallback.
        if project_path and ":" not in entry.overlay_class and entry.overlay_class:
            settings_module = entry.overlay_class
        else:
            settings_module = "teatree.settings"
        overlay_app = OverlayAppBuilder(entry.name, project_path, settings_module).build()
        app.add_typer(overlay_app, name=short_name)


# ── Entry point ──────────────────────────────────────────────────────


def _ensure_editable_if_contributing() -> None:
    """Auto-fix teatree and overlay to editable when contribute=true.

    When the user has ``contribute = true`` in ``~/.teatree.toml``, both
    teatree and the active overlay should be editable so local changes take
    effect immediately.  ``uv sync`` reinstalls from git, undoing this.
    This check runs on every CLI invocation and re-installs if needed.
    """
    try:
        from teatree.config import load_config  # noqa: PLC0415

        if not load_config().user.contribute:
            return

        if not IntrospectionHelpers.editable_info("teatree")[0]:
            repo = DoctorService.find_teatree_repo()
            if repo:
                DoctorService.make_editable("teatree", repo)

        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        for overlay_inst in get_all_overlays().values():
            overlay_module = type(overlay_inst).__module__
            top_package = overlay_module.split(".", maxsplit=1)[0]
            from importlib.metadata import packages_distributions  # noqa: PLC0415

            dist_map = packages_distributions()
            dist_names = dist_map.get(top_package, [top_package])
            overlay_dist = dist_names[0] if dist_names else top_package

            is_editable, _ = IntrospectionHelpers.editable_info(overlay_dist)
            if is_editable:
                continue
            overlay_repo = DoctorService.find_overlay_repo(overlay_dist)
            if overlay_repo:
                DoctorService.make_editable(overlay_dist, overlay_repo)
    except Exception:
        logger.debug("editable check skipped", exc_info=True)


def main() -> None:
    """Entry point for the ``t3`` console script."""
    _ensure_editable_if_contributing()
    register_overlay_commands()
    app(standalone_mode=True)
