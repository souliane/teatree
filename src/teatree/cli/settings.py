"""``t3 settings`` — thin CLI access to the settings management commands.

``compare`` is the Compare Instances page's own question asked from a terminal: what differs
between the boxes, and does it matter? It answers from
:func:`~teatree.core.settings.settings_compare.build_compare_view` — the SAME call the page makes — so
there is one comparison in the codebase and two ways to read it, never two implementations
that agree until one of them is changed.

The comparison touches ORM-backed snapshot state, so this module only bootstraps Django and
delegates to ``settings_compare``. The management command owns comparison and rendering.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

settings_app = typer.Typer(
    name="settings",
    no_args_is_help=True,
    help="This box's settings beside its peers' — the Compare Instances page, in a terminal.",
)

_SNAPSHOT_OPTION = typer.Option(
    None,
    "--snapshot",
    help=(
        "A saved snapshot JSON file to compare against, repeatable. A record reaches what a tunnel "
        "cannot: a box whose forward is down, a box that is gone, and this box as it stood earlier."
    ),
)
_KIND_OPTION = typer.Option(
    None,
    "--kind",
    help="Show only these difference kinds, repeatable: values, override, code. Default shows all three.",
)
_TIMEOUT_OPTION = typer.Option(
    None,
    "--timeout",
    help=(
        "Seconds to wait for each peer, overriding its registry entry. A forward under load "
        "answers late, and a wait too short reports a live box as one that did not answer."
    ),
)
_FULL_OPTION = typer.Option(False, "--full", help="Do not truncate a value, and print each row's import reason.")
_JSON_OPTION = typer.Option(False, "--json", help="Emit the whole view as JSON, nothing summarised away.")


@settings_app.command()
def compare(
    snapshot: list[str] = _SNAPSHOT_OPTION,
    kind: list[str] = _KIND_OPTION,
    timeout: float | None = _TIMEOUT_OPTION,
    *,
    full: bool = _FULL_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """What differs between this box and its peers, and whether it can be read as drift.

    Exits non-zero only when there was nothing to compare — fewer than two instances answered. A
    comparison that RAN is a success whatever it found, "not comparable" included: that verdict is
    a true reading of boxes running different code, not a failure of this command.

    The table is laid out in the terminal's own width, so ``COLUMNS`` sets it for a redirected run.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until Django is ready

    call_command(
        "settings_compare",
        snapshots=snapshot or [],
        kinds=kind or [],
        timeout=timeout,
        full=full,
        json_output=json_output,
    )


__all__ = ["settings_app"]
