"""TeaTree CLI — single ``t3`` entry point for all commands.

DB-touching commands are django-typer management commands, exposed here after
``django.setup()``.  Django-free commands live as plain Typer groups.

Each command (or small related cluster) lives in its own ``cli/<name>.py``
module.  This file holds only the Typer app construction, root callback,
sub-app registration, ``main`` entry point, and the project-root helpers
shared across modules.
"""

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.config import OverlayEntry

import teatree.cli.admin as _admin
import teatree.cli.agent as _agent
import teatree.cli.cost as _cost
import teatree.cli.info as _info
import teatree.cli.sessions as _sessions
import teatree.cli.speak as _speak
import teatree.cli.tokens as _tokens
import teatree.cli.ui as _ui
from teatree.cli import (
    comment_density_tools as _comment_density_tools,  # noqa: F401  (side-effect: registers comment-density onto tool_app)
)
from teatree.cli import (
    enforcement_tools as _enforcement_tools,  # noqa: F401  (side-effect: registers §17.6 gate commands onto tool_app)
)
from teatree.cli import (
    figma_tools as _figma_tools,  # noqa: F401  (side-effect: registers figma-* commands onto tool_app)
)
from teatree.cli import (
    skill_ref_tools as _skill_ref_tools,  # noqa: F401  (side-effect: registers validate-skill-refs onto tool_app)
)
from teatree.cli import (
    test_path_mirror_tools as _test_path_mirror_tools,  # noqa: F401  (side-effect: registers test-path-mirror onto tool_app)
)
from teatree.cli import (
    test_shape_tools as _test_shape_tools,  # noqa: F401  (side-effect: registers test-shape onto tool_app)
)
from teatree.cli import (
    triage_tools as _triage_tools,  # noqa: F401  (side-effect: registers triage commands onto tool_app)
)
from teatree.cli import verify_gates as _verify_gates  # noqa: F401  (side-effect: registers verify-gates onto tool_app)
from teatree.cli.assess import assess_app
from teatree.cli.banned_terms import banned_terms_app
from teatree.cli.ci import ci_app
from teatree.cli.codex import codex_app
from teatree.cli.config import config_app
from teatree.cli.doctor import DoctorService, IntrospectionHelpers, doctor_app
from teatree.cli.dogfood import dogfood_app
from teatree.cli.dream import dream_app
from teatree.cli.eval import eval_app
from teatree.cli.eval.skill_command_lane import register_command_registry_provider
from teatree.cli.identities import identities_app
from teatree.cli.loop import loop_app
from teatree.cli.loops import loops_app
from teatree.cli.mcp import mcp_app
from teatree.cli.mutation import mutation_app
from teatree.cli.overlay import OverlayAppBuilder
from teatree.cli.overlay_dev import overlay_dev_app
from teatree.cli.prompts import prompts_app
from teatree.cli.recover import recover_app
from teatree.cli.review import review_app, review_request_app
from teatree.cli.setup import setup_app
from teatree.cli.slack_listen import slack_app
from teatree.cli.task_alias import task_app
from teatree.cli.teams import teams_app
from teatree.cli.tools import tool_app
from teatree.cli.update import update_app
from teatree.cli.worker import worker

logger = logging.getLogger(__name__)

__all__ = ["app", "main"]

app = typer.Typer(name="t3", no_args_is_help=True, add_completion=False)


@app.callback()
def _root_callback(ctx: typer.Context) -> None:
    ctx.ensure_object(dict)
    _maybe_show_update_notice()


def _machine_readable_invocation() -> bool:
    """True when the CLI is producing machine-readable output.

    A consumer that runs ``t3 ... --json`` parses the output; the human
    update banner — even on stderr — corrupts that contract (a caller
    capturing combined output, or stderr, gets non-JSON noise). The
    ``--json`` flag (both ``--json`` and ``--json=...`` forms) is the
    canonical machine-readable signal across the teatree CLI.
    """
    return any(arg == "--json" or arg.startswith("--json=") for arg in sys.argv[1:])


def _maybe_show_update_notice() -> None:
    """Show update notice at most once per day, if enabled in user settings.

    Suppressed entirely in machine-readable invocations (``--json``) so
    the banner never pollutes parseable output (#719).
    """
    if _machine_readable_invocation():
        return
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
app.command()(_cost.cost)
app.command()(_tokens.tokens)
app.command()(_speak.speak)
app.add_typer(_info.info_app, name="info")
app.command()(_ui.ui)
app.command()(_admin.admin)
app.add_typer(config_app, name="config")
app.add_typer(banned_terms_app, name="banned-terms")
app.add_typer(ci_app, name="ci")
app.add_typer(codex_app, name="codex")
app.add_typer(review_app, name="review")
app.add_typer(review_request_app, name="review-request")
app.add_typer(eval_app, name="eval")
app.add_typer(doctor_app, name="doctor")
app.add_typer(tool_app, name="tool")
app.add_typer(setup_app, name="setup")
app.add_typer(update_app, name="update")
app.add_typer(assess_app, name="assess")
app.add_typer(overlay_dev_app, name="overlay")
app.add_typer(loop_app, name="loop")
app.command("worker")(worker)
app.add_typer(loops_app, name="loops")
app.add_typer(mcp_app, name="mcp")
app.add_typer(prompts_app, name="prompts")
app.add_typer(teams_app, name="teams")
app.add_typer(slack_app, name="slack")
app.add_typer(task_app, name="task")
app.add_typer(recover_app, name="recover")
app.add_typer(dogfood_app, name="dogfood")
app.add_typer(identities_app, name="identities")
app.add_typer(dream_app, name="dream")
app.add_typer(mutation_app, name="mutation")


# ── Django-dependent overlay command groups ───────────────────────────


def _collapse_to_canonical(entries: "list[OverlayEntry]") -> "list[OverlayEntry]":
    """Collapse entries sharing a canonical route key into one per key.

    A TOML overlay ``acme`` (with a path) and a ``t3-acme`` entry point both
    map to the ``acme`` route key; left distinct they would each register an
    ``acme`` Typer sub-app. The ``t3-``-prefixed entry-point form wins (it is
    the canonical installed overlay), inheriting a ``project_path`` from its
    bare TOML sibling when it lacks one.
    """
    from dataclasses import replace  # noqa: PLC0415

    from teatree.config import OverlayEntry  # noqa: PLC0415

    by_key: dict[str, OverlayEntry] = {}
    for entry in entries:
        key = OverlayEntry.canonical_overlay_name(entry.name)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = entry
            continue
        prefixed = entry if entry.name.startswith("t3-") else existing
        other = existing if prefixed is entry else entry
        if prefixed.project_path is None and other.project_path is not None:
            prefixed = replace(prefixed, project_path=other.project_path)
        by_key[key] = prefixed
    return list(by_key.values())


def register_overlay_commands(allowlist: set[str] | None = None) -> None:
    """Register all installed overlays as subcommand groups.

    No Django bootstrap needed — commands delegate to manage.py via subprocess.
    Pass *allowlist* of entry names (e.g. ``{"t3-teatree"}``) to register a subset —
    used by the CLI reference generator to keep generated docs deterministic.
    """
    from teatree.config import OverlayEntry, discover_active_overlay, discover_overlays  # noqa: PLC0415

    active = discover_active_overlay()
    installed = discover_overlays()

    registered: set[str] = set()
    for entry in _collapse_to_canonical(installed):
        if allowlist is not None and entry.name not in allowlist:
            continue
        short_name = OverlayEntry.canonical_overlay_name(entry.name)
        if short_name in registered:
            continue
        registered.add(short_name)
        project_path = entry.project_path or (active.project_path if active and active.name == entry.name else None)
        # Entry-point overlays use teatree base settings; TOML overlays with their own
        # project dir may have a settings module stored in overlay_class as fallback.
        if project_path and ":" not in entry.overlay_class and entry.overlay_class:
            settings_module = entry.overlay_class
        else:
            settings_module = "teatree.settings"
        overlay_app = OverlayAppBuilder(entry.name, project_path, settings_module).build()
        app.add_typer(overlay_app, name=short_name)


def _build_skill_command_registry() -> tuple[set[str], set[str]]:
    """The live ``(valid_paths, group_paths)`` for the #550 Tier-1 lane.

    Registers the ``teatree`` overlay's command group so the ``t3 teatree …``
    invocations skill docs cite resolve, then introspects the assembled root app.
    Lives here (the root CLI module) because the lane's dependency is inverted —
    ``teatree.cli.eval`` cannot import ``teatree.cli`` (cycle), so the parent
    injects this builder via ``register_command_registry_provider``.
    """
    from teatree.cli.command_tree import command_groups, command_paths  # noqa: PLC0415

    register_overlay_commands(allowlist={"t3-teatree"})
    return command_paths(app), command_groups(app)


register_command_registry_provider(_build_skill_command_registry)


# ── Entry point ──────────────────────────────────────────────────────


def _ensure_editable_if_contributing() -> None:
    """Auto-fix teatree and overlay to editable when contribute=true.

    When the user has ``contribute = true`` in ``~/.teatree.toml``, both
    teatree and the active overlay should be editable so local changes take
    effect immediately.  ``uv sync`` reinstalls from git, undoing this.
    This check runs on every CLI invocation and re-installs if needed.
    """
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415

        # ``contribute`` is DB-home (#1775); resolved via the effective-settings
        # tier. This runs before django.setup(), so the DB read fails safe to the
        # conservative default (False = skip auto-editable) when the DB is not yet
        # available — the user can still run ``t3 doctor`` to fix editability.
        if not get_effective_settings().contribute:
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


def main() -> None:  # pragma: no cover — console-script entry point (Typer dispatch glue)
    """Entry point for the ``t3`` console script."""
    _ensure_editable_if_contributing()
    register_overlay_commands()
    app(standalone_mode=True)
