"""``t3 loop directives`` — print the standing directives (read-only; #4166).

Split out of ``cli.loop.app`` (module-health), mirroring ``cli.loop.listing``.
Delegates to the ``loop_directives`` management command (anything touching the
ORM is a management command, not a plain typer command).
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def directives_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the standing directives as JSON."),
) -> None:
    """Print the standing directives every attended session is re-reminded of.

    Read-only. The ``--json`` payload — ``{slot_id, cadence_seconds, text,
    scope}`` per directive — is the harness-neutral contract: a non-Claude
    harness reads it and writes only its own delivery adapter.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_directives", **kwargs)


__all__ = ["directives_command"]
