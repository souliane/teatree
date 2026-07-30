"""``manage.py standing_goal`` — register / clear / list standing verified-green goals (PR-25).

Backs ``t3 goal`` (the ``set`` / ``clear`` / ``list`` subcommands). A standing goal is a named shell
``check_command`` whose zero exit means "green"; the Stop gate
``handle_standing_goal_stop`` re-runs it at turn-end and denies a stop-as-if-done
while an active goal is unmet. ORM access lives here (a management command, not a
plain typer command) per the project's "anything touching the ORM is a management
command" rule. Non-zero exits use ``raise SystemExit(N)`` (the ``call_command``
path); the CLI layer maps that to ``typer.Exit``.
"""

from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit


def _out_err(command: TyperCommand) -> tuple[IO[str], IO[str]]:
    return cast("IO[str]", command.stdout), cast("IO[str]", command.stderr)


def _set(command: TyperCommand, name: str, check: str, *, json_output: bool) -> None:
    from teatree.core.models import (  # noqa: PLC0415 — deferred: touch the ORM only at call time
        StandingGoal,
        StandingGoalError,
    )

    out, err = _out_err(command)
    try:
        goal = StandingGoal.objects.set_goal(name, check)
    except StandingGoalError as exc:
        emit({"ok": False, "error": str(exc)}, json_output=json_output, out=out, err=err, human=f"ERROR  {exc}")
        raise SystemExit(2) from exc
    emit(
        {"ok": True, "name": goal.name, "check_command": goal.check_command},
        json_output=json_output,
        out=out,
        err=err,
        human=f"OK    registered standing goal {goal.name!r} — green when `{goal.check_command}` exits 0.",
    )


def _clear(command: TyperCommand, name: str | None, *, json_output: bool) -> None:
    from teatree.core.models import StandingGoal  # noqa: PLC0415 — deferred import

    count = StandingGoal.objects.clear(name)
    scope = "all standing goals" if name is None else f"standing goal {name!r}"
    out, err = _out_err(command)
    emit(
        {"ok": True, "cleared": count, "scope": scope},
        json_output=json_output,
        out=out,
        err=err,
        human=f"OK    cleared {count} {scope}.",
    )


def _list(command: TyperCommand, *, json_output: bool) -> None:
    from teatree.core.models import StandingGoal  # noqa: PLC0415 — deferred import

    goals = list(StandingGoal.objects.order_by("created_at"))
    rows = [{"name": g.name, "check_command": g.check_command, "active": g.active} for g in goals]
    if not goals:
        human: str | None = "NOOP  no standing goals registered."
    else:
        human = "\n".join(
            f"{'active' if goal.active else 'retired':8}{goal.name} — `{goal.check_command}`" for goal in goals
        )
    out, err = _out_err(command)
    emit({"ok": True, "goals": rows}, json_output=json_output, out=out, err=err, human=human)


class Command(TyperCommand):
    help = "Register, clear, or list standing verified-green goals (PR-25)."

    @command(name="set")
    def set_goal(
        self,
        name: Annotated[str, typer.Argument(help="Unique goal name (e.g. 'evals-green').")],
        *,
        check: Annotated[str, typer.Option("--check", help="Shell command that exits 0 when the goal is green.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Register (or re-arm) a standing verified-green goal."""
        _set(self, name, check, json_output=json_output)

    @command(name="clear")
    def clear(
        self,
        name: Annotated[str | None, typer.Argument(help="Goal name to clear; omit to clear ALL.")] = None,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Delete one named standing goal, or every goal when no name is given."""
        _clear(self, name, json_output=json_output)

    @command(name="list")
    def list_goals(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """List every registered standing goal and its active state."""
        _list(self, json_output=json_output)
