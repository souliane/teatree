"""``t3 <overlay> speed`` — show / set the parallel-work throughput dial.

The ``speed`` dial (``slow`` < ``medium`` < ``full`` < ``boost``, default
``medium``) is a global ``[teatree]`` setting governing how many threads of
work the orchestrator drives at once — orthogonal to ``mode``/``autonomy``,
which gate *whether* a publish proceeds. The ``/t3:speed`` skill calls
``t3 <overlay> speed set <level>`` so the dial is persisted in one place
(``~/.teatree.toml``) rather than hand-edited.

``show`` reports the effective value (env / per-overlay / global / default
resolved via :func:`teatree.config.get_effective_settings`); ``set`` writes
the global ``[teatree] speed`` key. This is a pure-Python local read of the
resolver plus a ``tomlkit`` round-trip of ``~/.teatree.toml`` — it does NOT
route through Django or an overlay ``manage.py`` subprocess.
"""

from pathlib import Path

import typer

from teatree.config import Speed

SPEED_KEY = "speed"


def _config_path() -> Path:
    # Read at call time (not import) so a test monkeypatching
    # ``teatree.config.CONFIG_PATH`` is honoured.
    from teatree.config import CONFIG_PATH  # noqa: PLC0415

    return CONFIG_PATH


def _set_speed(level: Speed) -> None:
    # ``tomlkit`` is imported inline (matching ``teatree_gate``) so loading this
    # module — pulled when the overlay app is built — never eagerly imports the
    # toml-preserving dep.
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    config_path = _config_path()
    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()
    teatree = document.get("teatree")
    if not isinstance(teatree, tomlkit_items.Table):
        teatree = tomlkit.table()
        document["teatree"] = teatree
    teatree[SPEED_KEY] = level.value
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def register_speed_commands(overlay_app: typer.Typer) -> None:
    """Attach the ``speed`` subgroup (``show`` / ``set``) to an overlay app."""
    speed_group = typer.Typer(no_args_is_help=True, help="Parallel-work throughput dial.")

    @speed_group.command(name="show")
    def show() -> None:
        """Show the effective speed (env > per-overlay > global > default)."""
        from teatree.config import get_effective_settings  # noqa: PLC0415

        typer.echo(get_effective_settings().speed.value)

    @speed_group.command(name="set")
    def set_(level: str = typer.Argument(help="slow | medium | full | boost (aliases: low, normal, high)")) -> None:
        """Persist the global ``[teatree] speed`` dial. A typo is rejected."""
        try:
            parsed = Speed.parse(level)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        _set_speed(parsed)
        typer.echo(f'speed = {parsed.value} — wrote `{SPEED_KEY} = "{parsed.value}"` to {_config_path()}')

    overlay_app.add_typer(speed_group, name="speed")
