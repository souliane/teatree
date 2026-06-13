"""``t3 teams`` — the agent-teams master switch (on / off / status).

``[teams] enabled`` (default ``false``) is the single off switch for the
agent-teams pane layer (BLUEPRINT § 5.6 Track-B). When ``false``, the loop runs
the classic in-session ``Agent(run_in_background)`` sub-agent fan-out — no panes.
This group is the one obvious tag for that switch so it is persisted in one
place (``~/.teatree.toml``) rather than hand-edited.

The file value lives in the **top-level** ``[teams]`` table (not ``[teatree]``),
the namespace :func:`teatree.config.loader._resolve_teams_enabled` reads — so the
``tomlkit`` round-trip here writes exactly what the loader reads. ``status``
reports the **effective** value (env > per-overlay > global > default resolved
via :func:`teatree.config.get_effective_settings`).

Like the sibling ``speed`` / ``gate`` commands, this is a pure-Python local
read/modify/write of ``~/.teatree.toml`` — it does NOT route through Django or
an overlay ``manage.py`` subprocess, so it stays runnable independent of the
heavier overlay machinery. Kept top-level (like ``t3 loop``) because the switch
is overlay-agnostic.
"""

from pathlib import Path

import typer

TEAMS_TABLE = "teams"
ENABLED_KEY = "enabled"

CLASSIC_NOTE = "classic in-session sub-agent mode"

teams_app = typer.Typer(
    name="teams",
    help=(
        "Agent-teams master switch. The teams.enabled config key (default off) "
        "gates the pane-backed teammate layer; off keeps the classic in-session "
        "sub-agent fan-out."
    ),
    no_args_is_help=True,
)


def _config_path() -> Path:
    # Read at call time (not import) so a test monkeypatching
    # ``teatree.config.CONFIG_PATH`` is honoured.
    from teatree.config import CONFIG_PATH  # noqa: PLC0415

    return CONFIG_PATH


def _set_enabled(*, value: bool) -> None:
    # ``tomlkit`` is imported inline (matching ``speed`` / ``teatree_gate``) so
    # loading this module never eagerly imports the toml-preserving dep.
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    config_path = _config_path()
    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()
    teams = document.get(TEAMS_TABLE)
    if not isinstance(teams, tomlkit_items.Table):
        teams = tomlkit.table()
        document[TEAMS_TABLE] = teams
    teams[ENABLED_KEY] = value
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


@teams_app.command()
def on() -> None:
    """Enable agent teams — write teams.enabled = true to the config."""
    _set_enabled(value=True)
    typer.echo(f"agent teams = on — wrote `{ENABLED_KEY} = true` to {_config_path()}")


@teams_app.command()
def off() -> None:
    """Disable agent teams — write teams.enabled = false to the config."""
    _set_enabled(value=False)
    typer.echo(f"agent teams = off — wrote `{ENABLED_KEY} = false` to {_config_path()}")


@teams_app.command()
def status() -> None:
    """Show whether agent teams is on/off (effective value, read-only)."""
    from teatree.config import get_effective_settings  # noqa: PLC0415

    if get_effective_settings().teams_enabled:
        typer.echo("agent teams = on")
        return
    typer.echo(f"agent teams = off — {CLASSIC_NOTE}")
