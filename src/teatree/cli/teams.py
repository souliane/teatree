"""``t3 teams`` — the agent-teams master switch (on / off / status).

``teams_enabled`` (default ``false``) is the single off switch for the
agent-teams pane layer (BLUEPRINT § 5.6 Track-B). When ``false``, the loop runs
the classic in-session ``Agent(run_in_background)`` sub-agent fan-out — no panes.
This group is the one obvious tag for that switch so it is persisted in one
place rather than hand-edited.

``teams_enabled`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``on`` / ``off`` write a GLOBAL-scope DB row (a value
in the ``[teams]`` TOML table is ignored on read). ``status`` reports the
**effective** value (env > overlay-row > global-row > default resolved via
:func:`teatree.config.get_effective_settings`). The writes go through the ORM, so
they ensure Django is configured first. Kept top-level (like ``t3 loop``) because
the switch is overlay-agnostic.
"""

import typer

ENABLED_KEY = "teams_enabled"

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


def _set_enabled(*, value: bool) -> None:
    # Django/ORM imports are inline so building the overlay app (which loads this
    # module) never eagerly imports the model layer before settings are configured.
    from teatree.core.models import ConfigSetting  # noqa: PLC0415
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415

    ensure_django()
    ConfigSetting.objects.set_value(ENABLED_KEY, value)


@teams_app.command()
def on() -> None:
    """Enable agent teams — write the global teams_enabled = true config row."""
    _set_enabled(value=True)
    typer.echo(f"agent teams = on — wrote the {ENABLED_KEY} row to the global config store")


@teams_app.command()
def off() -> None:
    """Disable agent teams — write the global teams_enabled = false config row."""
    _set_enabled(value=False)
    typer.echo(f"agent teams = off — wrote the {ENABLED_KEY} row to the global config store")


@teams_app.command()
def status() -> None:
    """Show whether agent teams is on/off (effective value, read-only)."""
    from teatree.config import get_effective_settings  # noqa: PLC0415

    if get_effective_settings().teams_enabled:
        typer.echo("agent teams = on")
        return
    typer.echo(f"agent teams = off — {CLASSIC_NOTE}")
