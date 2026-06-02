"""``t3 <overlay> autonomy`` — show / set the per-overlay trust switch.

The ``autonomy`` switch (``babysit`` < ``notify`` < ``full``, default
``babysit``) is the single per-overlay knob that collapses the three
user-in-the-loop approval gates — ``on_behalf_post_mode`` (which gates
colleague auto-approve / on-behalf posts), ``require_human_approval_to_merge``,
``require_human_approval_to_answer`` — and pins ``mode = auto`` so the
loop's auto-merge path is reachable. The collapse and its precedence rules
live in :func:`teatree.config._apply_autonomy`; this command is the
first-class CLI surface that persists the knob in ``~/.teatree.toml`` so a
user flips an overlay to full merge/approve autonomy without hand-editing
TOML.

Because ``autonomy`` is per-overlay (``[overlays.<name>]``), ``set`` writes
the active overlay's table by default and accepts ``--overlay <name>`` to
target a specific one; ``--global`` writes the workspace-wide ``[teatree]``
default instead. ``show`` reports the effective value for the active overlay
(env / per-overlay / global / default resolved via
:func:`teatree.config.get_effective_settings`).

The knob NEVER touches the always-on safety/quality floor: independent
cold-review (maker != checker), CI-green-before-merge, the privacy/leak
gate, the never-lockout posture, and the substrate recorded-approver
keystone all stay in force under every tier. It only decides *whether to
ask the user*, never *whether the work is correct*.

This is a pure-Python local read of the resolver plus a ``tomlkit``
round-trip of ``~/.teatree.toml`` — it does NOT route through Django or an
overlay ``manage.py`` subprocess.
"""

from pathlib import Path

import typer

from teatree.config import Autonomy

AUTONOMY_KEY = "autonomy"


def _config_path() -> Path:
    # Read at call time (not import) so a test monkeypatching
    # ``teatree.config.CONFIG_PATH`` is honoured.
    from teatree.config import CONFIG_PATH  # noqa: PLC0415

    return CONFIG_PATH


def _active_overlay_name() -> str | None:
    from teatree.config import _active_overlay_entry  # noqa: PLC0415

    entry = _active_overlay_entry()
    return entry.name if entry is not None else None


def _set_global_autonomy(level: Autonomy) -> str:
    """Persist the workspace-wide ``[teatree] autonomy`` default; return its toml location."""
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    config_path = _config_path()
    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()
    table = document.get("teatree")
    if not isinstance(table, tomlkit_items.Table):
        table = tomlkit.table()
        document["teatree"] = table
    table[AUTONOMY_KEY] = level.value
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return "[teatree]"


def _set_overlay_autonomy(level: Autonomy, *, overlay: str) -> str:
    """Persist ``autonomy`` in the named overlay's ``[overlays.<name>]`` table; return its location.

    The per-overlay home matches the setting's resolution, so a value written
    here is what ``get_effective_settings()`` reads for that overlay.
    """
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    config_path = _config_path()
    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()
    overlays = document.get("overlays")
    if not isinstance(overlays, tomlkit_items.Table):
        overlays = tomlkit.table(is_super_table=True)
        document["overlays"] = overlays
    overlay_table = overlays.get(overlay)
    if not isinstance(overlay_table, tomlkit_items.Table):
        overlay_table = tomlkit.table()
        overlays[overlay] = overlay_table
    overlay_table[AUTONOMY_KEY] = level.value
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return f"[overlays.{overlay}]"


def register_autonomy_commands(overlay_app: typer.Typer) -> None:
    """Attach the ``autonomy`` subgroup (``show`` / ``set``) to an overlay app."""
    autonomy_group = typer.Typer(no_args_is_help=True, help="Per-overlay trust switch (collapses the approval gates).")

    @autonomy_group.command(name="show")
    def show() -> None:
        """Show the effective autonomy tier (env > per-overlay > global > default)."""
        from teatree.config import get_effective_settings  # noqa: PLC0415

        typer.echo(get_effective_settings().autonomy.value)

    @autonomy_group.command(name="set")
    def set_(
        level: str = typer.Argument(help="babysit | notify | full"),
        *,
        overlay: str = typer.Option(
            "",
            "--overlay",
            help="Overlay name to scope the value to (default: the active overlay). Ignored with --global.",
        ),
        write_global: bool = typer.Option(
            False,
            "--global",
            help="Write the workspace-wide [teatree] default instead of a per-overlay value.",
        ),
    ) -> None:
        """Persist the autonomy knob. A typo is rejected; the safety floor is never relaxed."""
        try:
            parsed = Autonomy.parse(level)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if write_global:
            location = _set_global_autonomy(parsed)
        else:
            target_overlay = overlay or _active_overlay_name()
            if not target_overlay:
                typer.echo(
                    "No active overlay to scope `autonomy` to; pass --overlay <name> or --global.",
                    err=True,
                )
                raise typer.Exit(code=1)
            location = _set_overlay_autonomy(parsed, overlay=target_overlay)
        typer.echo(
            f'autonomy = {parsed.value} — wrote `{AUTONOMY_KEY} = "{parsed.value}"` to {location} in {_config_path()}'
        )

    overlay_app.add_typer(autonomy_group, name="autonomy")
