"""``t3 <overlay> wip`` — show / set the bounded-WIP throughput dial.

The ``wip`` dial (``slow`` < ``medium`` < ``full`` < ``boost``, default
``medium``) governs how much new work a loop tick admits at once —
orthogonal to ``mode``/``autonomy``, which gate *whether* a publish proceeds.
The ``/t3:wip`` skill calls ``t3 <overlay> wip set <level>`` so the dial is
persisted in one place rather than hand-edited.

``wip`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a GLOBAL-scope DB row (a value in
``[teatree]`` TOML is ignored on read). ``show`` reports the effective value
(env / overlay-row / global-row / default resolved via
:func:`teatree.config.get_effective_settings`).

The typer overlay app is assembled in the ``t3`` console-script process, which
has NOT run ``django.setup()`` (souliane/teatree#2622). So ``set`` delegates the
ORM write to the ``config_setting`` management command via the same
``python -m teatree`` subprocess seam every other DB-touching overlay command
uses (:func:`teatree.cli.overlay.managepy_core`), and ``show`` bootstraps Django
before the resolver read — otherwise its ``ConfigSetting`` DB tier fails SAFE to
``{}`` and ``show`` silently reports the dataclass default instead of the
persisted dial.
"""

import json

import typer

from teatree.config import Wip

WIP_KEY = "wip"
BOOST_CONCURRENCY_KEY = "boost_concurrency"


def _set_wip(level: Wip) -> None:
    """Persist the GLOBAL-scope ``wip`` row via the ``config_setting`` management command.

    Delegates to ``python -m teatree config_setting set`` (:func:`teatree.cli.overlay.managepy_core`)
    so the ORM write runs in a process where ``django.setup()`` has been called,
    never in the unbootstrapped console-script process (#2622). The management
    command parses ``value`` as JSON, so the canonical value is JSON-encoded here.
    """
    from teatree.cli.overlay import managepy_core  # noqa: PLC0415

    managepy_core("config_setting", "set", WIP_KEY, json.dumps(level.value))


def _set_boost_concurrency(target: int) -> None:
    """Persist the GLOBAL-scope ``boost_concurrency`` row (same subprocess seam as ``_set_wip``)."""
    from teatree.cli.overlay import managepy_core  # noqa: PLC0415

    managepy_core("config_setting", "set", BOOST_CONCURRENCY_KEY, json.dumps(target))


def register_wip_commands(overlay_app: typer.Typer) -> None:
    """Attach the ``wip`` subgroup (``show`` / ``set``) to an overlay app."""
    wip_group = typer.Typer(no_args_is_help=True, help="Bounded-WIP throughput dial.")

    @wip_group.command(name="show")
    def show() -> None:
        """Show the effective wip (env > per-overlay > global > default)."""
        from teatree.config import get_effective_settings  # noqa: PLC0415
        from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415

        # ``get_effective_settings`` reads the ``ConfigSetting`` DB tier via the
        # app registry, which fails SAFE to ``{}`` when Django is not configured —
        # so without this bootstrap the console-script ``show`` reports the
        # dataclass DEFAULT instead of the persisted dial (#2622). Idempotent.
        ensure_django()
        settings = get_effective_settings()
        typer.echo(settings.wip.value)
        if settings.wip is Wip.BOOST and settings.boost_concurrency > 0:
            typer.echo(f"{BOOST_CONCURRENCY_KEY} = {settings.boost_concurrency}")

    @wip_group.command(name="set")
    def set_(level: str = typer.Argument(help="slow | medium | full | boost (aliases: low, normal, high)")) -> None:
        """Persist the global ``[teatree] wip`` dial. A typo is rejected."""
        try:
            parsed = Wip.parse(level)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        _set_wip(parsed)
        typer.echo(f"wip = {parsed.value} — wrote the {WIP_KEY} row to the global config store")

    @wip_group.command(name="boost")
    def boost(
        concurrency: int = typer.Argument(help="Target live worker count N the boost pool refills to."),
    ) -> None:
        """Arm boost mode with a live-worker target: sets ``wip = boost`` and ``boost_concurrency = N``.

        The pool-refill driver then keeps ``N`` loop workers in flight — when a
        worker exits below ``N`` the next tick admits the shortfall. ``N`` is
        clamped at admission by the PR-01 resource concurrency ceiling.
        """
        if concurrency < 1:
            typer.echo(f"boost_concurrency must be a positive integer, got {concurrency}", err=True)
            raise typer.Exit(code=1)
        _set_wip(Wip.BOOST)
        _set_boost_concurrency(concurrency)
        typer.echo(f"wip = {Wip.BOOST.value}, {BOOST_CONCURRENCY_KEY} = {concurrency} — wrote both rows")

    overlay_app.add_typer(wip_group, name="wip")
