"""``manage.py shipped_seed`` — audit the shipped seed set, and delete from it safely (#3842).

Backs ``t3 loops audit`` and the audited delete verbs. Two halves of one concern: which
shipped loops/presets/schedules are missing, off, not ticking, or running a value that no
longer matches what ``defaults.toml`` ships (:mod:`teatree.loops.seed_inertness`), and the
typed confirm that gates removing one (:mod:`teatree.loops.shipped_guard`). ORM access
lives in a management command (the project's "anything touching the ORM is a management
command" rule).

``audit`` exits NON-ZERO on any fault via ``SystemExit`` — never ``typer.Exit``, which
``call_command`` swallows so the process would exit 0 on a real failure.
"""

from collections.abc import Callable
from typing import IO, Annotated, NoReturn, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.loops.loop_admin import delete_loop
from teatree.loops.preset_admin import delete_preset
from teatree.loops.preset_editing import PresetEditError
from teatree.loops.schedule_editing import delete_schedule
from teatree.loops.seed_inertness import InertFinding, shipped_inertness

_FAMILY_WIDTH = 9
_NAME_WIDTH = 22
_KIND_WIDTH = 24


def _render(findings: tuple[InertFinding, ...]) -> str:
    faults = [f for f in findings if f.is_fault]
    notes = [f for f in findings if not f.is_fault]
    lines = [
        _block(
            "FAULTS",
            faults,
            empty="every shipped loop, preset and schedule is present, firing, and drains what it fills.",
        )
    ]
    if notes:
        lines.append(_block("NOTES (deliberate — not failures)", notes, empty=""))
    return "\n\n".join(block for block in lines if block)


def _block(title: str, findings: list[InertFinding], *, empty: str) -> str:
    if not findings:
        return f"OK  {empty}" if empty else ""
    rows = "\n".join(
        f"  {f.family:<{_FAMILY_WIDTH}} {f.name:<{_NAME_WIDTH}} {f.kind:<{_KIND_WIDTH}} {f.detail}" for f in findings
    )
    return f"{title} ({len(findings)}):\n{rows}"


class Command(TyperCommand):
    help = "Audit the shipped loop/preset/schedule seed set, and delete from it with a typed confirm (#3842)."

    @command(name="audit")
    def audit(self, *, json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False) -> None:
        """Report every shipped definition missing, disabled, not ticking, or diverged from shipped.

        The expected set is read from the shipped seed tables, not from the DB — a check
        that reads the DB for both sides cannot see a row that was deleted, nor a value
        edited away from the one that ships.
        """
        findings = shipped_inertness()
        faults = [f for f in findings if f.is_fault]
        emit(
            {"findings": [f.as_json() for f in findings], "fault_count": len(faults)},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render(findings),
        )
        if faults:
            raise SystemExit(1)

    @command(name="delete-loop")
    def delete_loop_command(
        self,
        name: Annotated[str, typer.Argument(help="Loop to delete.")] = "",
        *,
        confirm: Annotated[str, typer.Option("--confirm", help="Typed phrase; required for a shipped loop.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Delete a loop row — a shipped one needs ``--confirm stop-<name>``."""
        self._delete(lambda: delete_loop(name, confirm=confirm), "loop", name, json_output=json_output)

    @command(name="delete-schedule")
    def delete_schedule_command(
        self,
        name: Annotated[str, typer.Argument(help="Schedule to delete.")] = "",
        *,
        confirm: Annotated[str, typer.Option("--confirm", help="Typed phrase; required for a shipped schedule.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Delete a calendar and its slots — a shipped one needs ``--confirm stop-<name>``."""
        self._delete(lambda: delete_schedule(name, confirm=confirm), "schedule", name, json_output=json_output)

    @command(name="delete-preset")
    def delete_preset_command(
        self,
        name: Annotated[str, typer.Argument(help="Preset to delete.")] = "",
        *,
        confirm: Annotated[str, typer.Option("--confirm", help="Typed phrase; required for a shipped preset.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Delete a preset — a shipped one needs ``--confirm stop-<name>``."""
        self._delete(lambda: delete_preset(name, confirm=confirm), "preset", name, json_output=json_output)

    def _delete(self, action: Callable[[], None], family: str, name: str, *, json_output: bool) -> None:
        if not name.strip():
            self._refuse(f"a {family} name is required", json_output=json_output)
        try:
            action()
        except PresetEditError as exc:
            self._refuse(str(exc), json_output=json_output)
        emit(
            {"deleted": name, "family": family},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"deleted {family} {name!r}. `t3 setup` recreates it if it ships by default.",
        )

    def _refuse(self, message: str, *, json_output: bool) -> NoReturn:
        emit(
            {"error": message},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"ERROR  {message}",
        )
        raise SystemExit(1)
