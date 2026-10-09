"""``t3 loop claim-next`` — the atomic-claim CLI command.

This wrapper bootstraps Django and delegates to the ``loop_dispatch claim-next``
management command. The assembled callable is registered on the parent
``loop_app`` without a circular import.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def claim_next_command(
    *,
    claimed_by: str = typer.Option("", "--claimed-by", help="Worker identifier stored on the claim."),
    claimed_by_session: str | None = typer.Option(
        None,
        "--claimed-by-session",
        help="Worker session id stored on the claim (defaults to the active session, empty when none).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the claimed dispatch as JSON."),
) -> None:
    """Atomically claim the oldest pending dispatchable Task, then emit it."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.loop.session_identity import current_session_id  # noqa: PLC0415 — deferred: keeps CLI startup light

    session = current_session_id() if claimed_by_session is None else claimed_by_session
    kwargs: dict[str, str | bool] = {"claimed_by_session": session}
    if claimed_by:
        kwargs["claimed_by"] = claimed_by
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_dispatch", "claim-next", **kwargs)
