"""``t3 dogfood`` — overlay-smoke management commands (#1308).

The loop reaches for an overlay's provision path only when the user
needs E2E, so latent CLI bugs accumulate quietly between runs and
surface as a cascade at the worst possible time.
``t3 dogfood overlay-provision-smoke`` exercises the canonical
provision path end-to-end against a fixture ticket so bugs surface in
the loop's tick (and DM the user immediately), not mid-E2E session.

Subcommands:

* ``overlay-provision-smoke`` — run the smoke against ``--overlay`` and
    exit 0 on PASS, non-zero with a categorised failure
    (``provision_failed`` / ``start_failed`` / ``ready_failed`` /
    ``teardown_failed`` / ``clean_failed`` / ``overlay_resolution_failed``
    / ``timeout``). DMs the user with the failing command + stderr on any
    non-PASS outcome.

``overlay_resolution_failed`` is this class: the workspace steps run
once with ``T3_OVERLAY_NAME`` unset (a bare ``get_overlay()`` raised
``Multiple overlays found`` on a multi-overlay install) and once with it
pinned, so a regression in either resolution route is categorised on its own.

A PASS also names what it could NOT exercise (``uncovered: ...``) — an
overlay declaring no non-identity variant cannot prove the DSLR
variant→tenant alias path, and that gap is reported rather than hidden
behind a green run.

The room for sibling smokes is the ``t3 dogfood`` namespace itself —
add subcommands here.

Non-zero exits use ``raise SystemExit(N)``, never ``typer.Exit`` — these
commands run under Django's ``call_command`` (django-typer), which swallows a
``typer.Exit`` into a *returned* code and exits 0, so a categorised failure
would silently report success to cron/CI/the loop. ``SystemExit`` propagates.
"""

import logging
from typing import Annotated

import typer
from django.core.exceptions import ImproperlyConfigured
from django_typer.management import TyperCommand, command, initialize

from teatree.config import OverlayEntry
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Ticket
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.overlay_loader import get_overlay, resolve_overlay_name
from teatree.loop.dogfood_smoke import (
    SmokeOutcomeKind,
    WorktreePathResolver,
    default_steps,
    pick_alias_variant,
    report_summary,
    run_smoke,
    run_t3_command,
)


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
    from teatree.core.notify import NotifyKind  # noqa: PLC0415 — deferred: keeps command import light
    from teatree.messaging import notify_with_fallback  # noqa: PLC0415 — deferred: keeps command import light

    body = _dm_failure_body(summary_text, failing_step=failing_step, command_str=command_str, stderr=stderr)
    key = f"dogfood_smoke:{failing_step}"
    try:
        notify_with_fallback(body, kind=NotifyKind.INFO, idempotency_key=key, audience=NotifyAudience.OWNER_ESCALATION)
    except Exception:
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
        SmokeOutcomeKind.OVERLAY_RESOLUTION_FAILED: 17,
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
            typer.Option(
                help=(
                    "Overlay the smoke targets — either the CLI sub-app's short name (teatree) or"
                    " the dispatchable Ticket.overlay form the scanner queues (t3-teatree)."
                )
            ),
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
        notify_on_failure: Annotated[  # noqa: FBT002 — typer CLI boolean flag; the bool parameter is typer's option idiom
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
        :func:`teatree.core.notify.notify_user` on any non-PASS outcome
        unless ``--no-notify-on-failure`` is passed (CI hook).
        """
        target_overlay = overlay or _resolve_active_overlay()
        if not target_overlay:
            typer.echo("error: no overlay resolved — pass --overlay <name>.", err=True)
            raise SystemExit(2)

        # get_overlay() needs the dispatchable Ticket.overlay form (t3-teatree);
        # the shelled ``t3 <overlay> ...`` steps below need the CLI sub-app's short form (teatree).
        dispatch_overlay = resolve_overlay_name(target_overlay) or target_overlay
        cli_overlay = OverlayEntry.canonical_overlay_name(dispatch_overlay)

        smoke_variant, uncovered = _resolve_smoke_variant(dispatch_overlay, variant)
        steps = default_steps(
            overlay=cli_overlay,
            fixture_ticket_url=fixture_ticket_url,
            variant=smoke_variant,
        )
        if dry_run:
            typer.echo("[dogfood] dry-run — planned steps:")
            for step in steps:
                typer.echo(f"  {step.name}: {' '.join(step.command)}")
            for item in uncovered:
                typer.echo(f"  uncovered: {item}")
            return

        report = run_smoke(
            steps,
            runner=run_t3_command,
            uncovered=uncovered,
            resolve_worktree_path=_worktree_path_resolver(
                issue_url=fixture_ticket_url,
                overlay=dispatch_overlay,
            ),
        )
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


#: Named in the smoke summary when the run could not exercise the DSLR alias path.
ALIAS_VARIANT_UNCOVERED = "dslr-alias-variant"


def _resolve_smoke_variant(overlay_name: str, explicit: str) -> tuple[str, list[str]]:
    """Resolve the variant the smoke provisions, plus what it could not cover.

    An explicit ``--variant`` always wins. Otherwise the overlay's own
    non-identity alias is picked so the run exercises the variant→tenant
    lookup (#1308); an overlay declaring none leaves that acceptance item
    UNCOVERED rather than passing as if it had been proven.
    """
    if explicit:
        return explicit, []
    try:
        overlay = get_overlay(overlay_name)
    except ImproperlyConfigured:
        # Only the not-registered case is an uncovered acceptance item. A bare `except
        # Exception` here turned every configuration error into this same silent answer,
        # which is what let three tests patch a name the function never reads and still pass.
        logging.getLogger(__name__).exception("Could not load overlay %s to pick an alias variant", overlay_name)
        return "", [f"{ALIAS_VARIANT_UNCOVERED} (overlay {overlay_name} not loadable)"]
    alias = pick_alias_variant(overlay)
    if not alias:
        return "", [f"{ALIAS_VARIANT_UNCOVERED} (overlay {overlay_name} declares no non-identity variant)"]
    return alias, []


def _worktree_path_resolver(*, issue_url: str, overlay: str) -> WorktreePathResolver:
    """Answer where ``workspace_ticket`` put the worktree, read at call time.

    Deferred behind a closure because the answer does not exist until that step
    has run: the ticket row is created by the shelled-out ``t3 <overlay>
    workspace ticket``, in a child process, so it can only be read back from the
    DB afterwards. Keyed on the DISPATCHABLE overlay form, which is what
    ``Ticket.overlay`` stores; '' when no row or no materialised checkout exists,
    which :func:`run_smoke` turns into a categorised failure.
    """

    def resolve() -> str:
        ticket = Ticket.objects.filter(issue_url=issue_url, overlay=overlay).order_by("-pk").first()
        return dispatch_worktree_path(ticket) if ticket is not None else ""

    return resolve


def _resolve_active_overlay() -> str:
    """Return the active overlay short name, or empty string when none is registered."""
    from teatree.config import discover_active_overlay  # noqa: PLC0415 — deferred: lazy command import

    active = discover_active_overlay()
    if active is None:
        return ""
    return OverlayEntry.canonical_overlay_name(active.name)
