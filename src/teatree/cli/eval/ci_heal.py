"""``t3 eval ci-heal`` — operator control of the CI-eval self-healing loop (#3201 PR-3a).

The loop is default-OFF and never discovers PR branches on its own; an operator
OPENS a heal session for a branch here, and the (enabled) mini-loop then advances
it — dispatch a CI eval, poll, GREEN / HALT + escalate. The subcommands:

* ``open --ref <branch>`` — create a ``pending`` :class:`~teatree.core.models.CiEvalHealSession`.
* ``list`` — the open + recent sessions and their state.
* ``advance`` — run ONE advance pass by hand (an operator dry-run that exercises the
    exact loop step WITHOUT enabling the autonomous ``Loop`` row); reaches ``gh``.

Never a fix: this observe surface can only dispatch, poll, GREEN, or HALT+escalate.
"""

import dataclasses
import json

import typer

from teatree.types import RawAPIDict
from teatree.utils.django_bootstrap import ensure_django

ci_heal_app = typer.Typer(
    no_args_is_help=True,
    help="Operator control of the CI-eval self-healing loop (open sessions, list, dry-run advance).",
)

_DEFAULT_MAX_FIX_ATTEMPTS = 2


@dataclasses.dataclass(frozen=True)
class _SessionRow:
    """One session rendered for the operator — the durable FSM state, publish-safe."""

    id: int
    overlay: str
    pr_ref: str
    state: str
    head_sha: str
    red_scenarios: list[str]
    fix_attempts: int
    max_fix_attempts: int
    halt_reason: str

    def as_json(self) -> RawAPIDict:
        return dataclasses.asdict(self)


def _row(session: object) -> _SessionRow:
    return _SessionRow(
        id=session.pk,  # type: ignore[attr-defined]
        overlay=session.overlay,  # type: ignore[attr-defined]
        pr_ref=session.pr_ref,  # type: ignore[attr-defined]
        state=session.state,  # type: ignore[attr-defined]
        head_sha=session.head_sha,  # type: ignore[attr-defined]
        red_scenarios=list(session.red_scenarios),  # type: ignore[attr-defined]
        fix_attempts=session.fix_attempts,  # type: ignore[attr-defined]
        max_fix_attempts=session.max_fix_attempts,  # type: ignore[attr-defined]
        halt_reason=session.halt_reason,  # type: ignore[attr-defined]
    )


@ci_heal_app.command("open")
def open_session(
    ref: str = typer.Option(..., "--ref", help="PR branch to open a CI-eval heal session for."),
    overlay: str = typer.Option("", "--overlay", help="Overlay the branch belongs to (default: the core overlay)."),
    max_fix_attempts: int = typer.Option(
        _DEFAULT_MAX_FIX_ATTEMPTS,
        "--max-fix-attempts",
        help="Bounded fix budget the PR-3b autonomous fixer honours (observe-only ignores it).",
    ),
) -> None:
    """Open a pending heal session for a PR branch and print it as JSON."""
    ensure_django()
    from teatree.core.models import CiEvalHealSession  # noqa: PLC0415 — deferred: ORM needs the app registry

    session = CiEvalHealSession.objects.open_session(
        overlay=overlay,
        pr_ref=ref,
        max_fix_attempts=max(1, max_fix_attempts),
    )
    typer.echo(json.dumps(_row(session).as_json(), indent=2))


@ci_heal_app.command("list")
def list_sessions(
    output_json: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False, "--json", help="Emit the sessions as a JSON array."
    ),
    limit: int = typer.Option(50, "--limit", help="Most-recent N sessions to show."),
) -> None:
    """List the recent CI-eval heal sessions and their FSM state."""
    ensure_django()
    from teatree.core.models import CiEvalHealSession  # noqa: PLC0415 — deferred: ORM needs the app registry

    rows = [_row(session) for session in CiEvalHealSession.objects.all()[: max(1, limit)]]
    if output_json:
        typer.echo(json.dumps([row.as_json() for row in rows], indent=2))
        return
    if not rows:
        typer.echo("(no CI-eval heal sessions)")
        return
    for row in rows:
        reds = f" reds={len(row.red_scenarios)}" if row.red_scenarios else ""
        typer.echo(f"#{row.id} {row.pr_ref} [{row.state}]{reds}")


@ci_heal_app.command("advance")
def advance(
    output_json: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False, "--json", help="Emit the advance outcomes as a JSON array."
    ),
) -> None:
    """Run ONE advance pass over every open session by hand (an operator dry-run; reaches gh)."""
    ensure_django()
    from teatree.loop.ci_eval_heal_advance import advance_open_sessions  # noqa: PLC0415 — deferred: ORM-reaching

    run = advance_open_sessions()
    if output_json:
        payload = {
            "outcomes": [dataclasses.asdict(outcome) for outcome in run.outcomes],
            "errors": run.errors,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    if not run.outcomes and not run.errors:
        typer.echo("(no open CI-eval heal sessions)")
        return
    for outcome in run.outcomes:
        typer.echo(f"{outcome.pr_ref}: {outcome.from_state} -> {outcome.to_state} ({outcome.note})")
    for key, error in run.errors.items():
        typer.echo(f"{key}: {error}", err=True)
