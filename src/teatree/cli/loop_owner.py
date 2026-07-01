"""``t3 loop claim/owner/release`` — pilot the session-scoped t3-master (#1073).

Split out of ``cli.loop`` (module-health: that file owns the tick / start /
dashboard / self-improve concerns; loop-ownership hand-off is a distinct
concern). :func:`register` attaches the three flat ``t3 loop`` subcommands
onto the shared ``loop_app`` so the user-facing surface stays
``t3 loop claim`` (not ``t3 loop owner claim``). Each delegates to the
``loop_owner`` Django management command — anything touching the ORM is a
management command, not a plain typer command.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def _delegate(subcommand: str, *, slot: str | None, json_output: bool, extra: dict[str, bool] | None = None) -> None:
    """Call ``loop_owner <subcommand>``; map a mgmt-command ``SystemExit`` to ``typer.Exit``.

    ``loop_owner`` raises ``SystemExit(N)`` (correct on the Django
    ``call_command`` path); the CLI layer must surface that as a
    ``typer.Exit`` so the process exit code is preserved. ``slot=None`` for
    slot-agnostic subcommands (``whoami``) that take no ``--slot`` arg.
    """
    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str | bool] = {}
    if slot is not None:
        kwargs["slot"] = slot
    if json_output:
        kwargs["json_output"] = True
    if extra:
        kwargs.update(extra)
    try:
        call_command("loop_owner", subcommand, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


def register(loop_app: typer.Typer) -> None:
    """Attach ``claim`` / ``owner`` / ``release`` onto the shared loop Typer."""

    @loop_app.command("claim")
    def claim_command(
        *,
        take_over: bool = typer.Option(
            False,
            "--take-over",
            help="Evict a live claimant — the chat-only user's loop hand-off (#1073).",
        ),
        slot: str = typer.Option("t3-master", "--slot", help="t3-master slot name (default: t3-master)."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Claim the session-scoped t3-master slot for this Claude session (#1073).

        Without ``--take-over`` a live claimant blocks the claim. With it,
        the claim is unconditional — the hijacking session's next ``t3 loop
        tick`` SKIPs within one tick, no restart needed. Exits 2 when not
        running inside a Claude Code session (no session id to claim with).
        """
        _delegate("claim", slot=slot, json_output=json_output, extra={"take_over": True} if take_over else None)

    @loop_app.command("owner")
    def owner_command(
        *,
        slot: str = typer.Option("t3-master", "--slot", help="t3-master slot name (default: t3-master)."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Show which session owns the t3-master slot AND this session's own id (#1073)."""
        _delegate("owner", slot=slot, json_output=json_output)

    @loop_app.command("whoami")
    def whoami_command(
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Print this Claude session's own id — what a hand-off ``--to`` targets."""
        _delegate("whoami", slot=None, json_output=json_output)

    @loop_app.command("release")
    def release_command(
        *,
        slot: str = typer.Option("t3-master", "--slot", help="t3-master slot name (default: t3-master)."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Release this session's t3-master claim (#1073).

        CAS on session id — a non-owner release is a no-op and never evicts
        a live owner.
        """
        _delegate("release", slot=slot, json_output=json_output)


__all__ = ["register"]
