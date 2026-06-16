"""``t3 <overlay> speed`` — show / set the parallel-work throughput dial.

The ``speed`` dial (``slow`` < ``medium`` < ``full`` < ``boost``, default
``medium``) governs how many threads of work the orchestrator drives at once —
orthogonal to ``mode``/``autonomy``, which gate *whether* a publish proceeds.
The ``/t3:speed`` skill calls ``t3 <overlay> speed set <level>`` so the dial is
persisted in one place rather than hand-edited.

``speed`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a GLOBAL-scope DB row (a value in
``[teatree]`` TOML is ignored on read). ``show`` reports the effective value
(env / overlay-row / global-row / default resolved via
:func:`teatree.config.get_effective_settings`). ``set`` writes through the ORM,
so it ensures Django is configured first.
"""

import typer

from teatree.config import Speed

SPEED_KEY = "speed"


def _set_speed(level: Speed) -> None:
    # Django/ORM imports are inline so building the overlay app (which loads this
    # module) never eagerly imports the model layer before settings are configured.
    from teatree.core.models import ConfigSetting  # noqa: PLC0415
    from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415

    ensure_django()
    ConfigSetting.objects.set_value(SPEED_KEY, level.value)


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
        typer.echo(f"speed = {parsed.value} — wrote the {SPEED_KEY} row to the global config store")

    overlay_app.add_typer(speed_group, name="speed")
