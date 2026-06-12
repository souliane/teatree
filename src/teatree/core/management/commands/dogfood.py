"""``t3 dogfood`` — overlay-smoke management commands (#1308).

The fat loop reaches for an overlay's provision path only when the user
needs E2E, so latent CLI bugs accumulate quietly between runs and
surface as a cascade at the worst possible time.
``t3 dogfood overlay-provision-smoke`` exercises the canonical
provision path end-to-end against a fixture ticket so bugs surface in
the loop's tick (and DM the user immediately), not mid-E2E session.

Subcommands:

* ``overlay-provision-smoke`` — run the smoke against ``--overlay`` and
    exit 0 on PASS, non-zero with a categorised failure
    (``provision_failed`` / ``start_failed`` / ``ready_failed`` /
    ``teardown_failed`` / ``clean_failed`` / ``timeout``). DMs the user
    with the failing command + stderr on any non-PASS outcome.

The room for sibling smokes is the ``t3 dogfood`` namespace itself —
add subcommands here.

Non-zero exits use ``raise SystemExit(N)``, never ``typer.Exit`` — these
commands run under Django's ``call_command`` (django-typer), which swallows a
``typer.Exit`` into a *returned* code and exits 0, so a categorised failure
would silently report success to cron/CI/the loop. ``SystemExit`` propagates.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.loop.dogfood_smoke import SmokeOutcomeKind, default_steps, report_summary, run_smoke, run_t3_command


def _dm_failure_body(report_summary_text: str, *, failing_step: str, command_str: str, stderr: str) -> str:
    """Compose the failure DM body — short, scannable, command-ready."""
    body = [
        f":rotating_light: {report_summary_text}",
        f"step: `{failing_step}`",
        f"command: `{command_str}`",
    ]
    if stderr.strip():
        tail = "\n".join(stderr.strip().splitlines()[-10:])
        body.extend(("stderr:", f"```\n{tail}\n```"))
    return "\n".join(body)


def _notify_failure(*, summary_text: str, failing_step: str, command_str: str, stderr: str) -> None:
    """DM the user that the smoke failed.

    Best-effort: any failure inside the notify path is logged and
    swallowed so a notify failure never propagates out of the smoke
    command (the CLI exit code already carries the verdict).
    """
    from teatree.core.notify import NotifyKind  # noqa: PLC0415
    from teatree.messaging import notify_with_fallback  # noqa: PLC0415

    body = _dm_failure_body(summary_text, failing_step=failing_step, command_str=command_str, stderr=stderr)
    key = f"dogfood_smoke:{failing_step}"
    try:
        notify_with_fallback(body, kind=NotifyKind.INFO, idempotency_key=key)
    except Exception:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).exception("Failed to DM user about dogfood smoke failure")


def _exit_code_for(outcome: SmokeOutcomeKind) -> int:
    """Map a smoke outcome to the CLI exit code.

    PASS → 0; everything else is a non-zero exit code keyed off the
    outcome so cron/CI logs can grep by exit status alone.
    """
    if outcome is SmokeOutcomeKind.PASS:
        return 0
    return {
        SmokeOutcomeKind.PROVISION_FAILED: 11,
        SmokeOutcomeKind.START_FAILED: 12,
        SmokeOutcomeKind.READY_FAILED: 13,
        SmokeOutcomeKind.TEARDOWN_FAILED: 14,
        SmokeOutcomeKind.CLEAN_FAILED: 15,
        SmokeOutcomeKind.TIMEOUT: 16,
        SmokeOutcomeKind.UNKNOWN: 19,
    }.get(outcome, 1)


class Command(TyperCommand):
    help = "Dogfood overlay smokes — exercise CLI paths so bugs surface in the loop, not in E2E."

    @initialize()
    def init(self) -> None:
        """``t3 dogfood`` group root."""

    @command()
    def overlay_provision_smoke(
        self,
        overlay: Annotated[
            str,
            typer.Option(help="Overlay short name the smoke targets (e.g. the CLI sub-app under t3)."),
        ] = "",
        fixture_ticket_url: Annotated[
            str,
            typer.Option(help="Fixture ticket URL the smoke binds against."),
        ] = "https://github.com/souliane/teatree/issues/1308",
        variant: Annotated[
            str,
            typer.Option(help="Overlay tenant variant the smoke provisions (empty = overlay's default)."),
        ] = "",
        dry_run: Annotated[  # noqa: FBT002 — typer convention; bool flag with default
            bool,
            typer.Option("--dry-run", help="Print the planned steps and exit 0 without executing them."),
        ] = False,
        notify_on_failure: Annotated[  # noqa: FBT002
            bool,
            typer.Option(
                "--notify-on-failure/--no-notify-on-failure",
                help="DM the user on failure (default: on; use --no-notify-on-failure in CI).",
            ),
        ] = True,
    ) -> None:
        """Run the overlay provision smoke against a fixture ticket.

        Exits 0 on PASS, 11-19 on categorised failure (see
        :func:`_exit_code_for`) via ``raise SystemExit(code)`` so the code
        propagates under ``call_command``. DMs the user via
        :func:`teatree.notify.notify_user` on any non-PASS outcome
        unless ``--no-notify-on-failure`` is passed (CI hook).
        """
        target_overlay = overlay or _resolve_active_overlay()
        if not target_overlay:
            typer.echo("error: no overlay resolved — pass --overlay <name>.", err=True)
            raise SystemExit(2)

        steps = default_steps(
            overlay=target_overlay,
            fixture_ticket_url=fixture_ticket_url,
            variant=variant,
        )
        if dry_run:
            typer.echo("[dogfood] dry-run — planned steps:")
            for step in steps:
                typer.echo(f"  {step.name}: {' '.join(step.command)}")
            return

        report = run_smoke(steps, runner=run_t3_command)
        summary = report_summary(report)
        typer.echo(summary)

        if not report.passed and notify_on_failure:
            failing = report.failing_step
            command_str = ""
            for result in report.steps:
                if result.step.name == failing:
                    command_str = " ".join(result.step.command)
                    break
            _notify_failure(
                summary_text=summary,
                failing_step=failing,
                command_str=command_str,
                stderr=report.failing_step_stderr,
            )

        code = _exit_code_for(report.outcome)
        if code != 0:
            raise SystemExit(code)


def _resolve_active_overlay() -> str:
    """Return the active overlay short name, or empty string when none is registered."""
    from teatree.config import OverlayEntry, discover_active_overlay  # noqa: PLC0415

    active = discover_active_overlay()
    if active is None:
        return ""
    return OverlayEntry.canonical_overlay_name(active.name)
