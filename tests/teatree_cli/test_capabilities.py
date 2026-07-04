"""PR-30 ``t3 capabilities`` — the machine-readable capability registry.

Guards that the registry is well-formed, that ``--json`` emits it as pure JSON
on stdout, and (non-vacuously) that the commands the registry marks ``json:true``
for the PR-30 conversions actually declare a ``json_output`` parameter — so the
registry cannot drift from the commands it documents.
"""

import inspect
import json

from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.capabilities import CAPABILITIES, capabilities_report


class TestRegistryWellFormed:
    def test_no_duplicate_commands(self) -> None:
        names = [cap.command for cap in CAPABILITIES]
        assert len(names) == len(set(names))

    def test_every_capability_has_exit_codes(self) -> None:
        assert all(cap.exit_codes for cap in CAPABILITIES)

    def test_report_round_trips_through_json(self) -> None:
        report = capabilities_report()
        assert json.loads(json.dumps(report)) == report
        assert {"version", "exit_code_contract", "commands"} <= set(report)


class TestCapabilitiesCommand:
    def test_json_emits_pure_json_on_stdout(self) -> None:
        result = CliRunner().invoke(app, ["capabilities", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)  # raises if the human listing leaked onto stdout
        assert parsed == capabilities_report()

    def test_non_json_lists_the_commands(self) -> None:
        result = CliRunner().invoke(app, ["capabilities"])
        assert result.exit_code == 0
        assert "queue status" in result.output


class TestRegistryMatchesConvertedCommands:
    """The registry cannot drift from the commands it documents.

    Non-vacuity: the PR-30-converted commands the registry claims support
    ``--json`` must actually declare a ``json_output`` parameter.
    """

    def test_queue_status_declares_json_output(self) -> None:
        from teatree.core.management.commands.queue import Command  # noqa: PLC0415

        assert "json_output" in inspect.signature(Command.status).parameters

    def test_tasks_list_declares_json_output(self) -> None:
        from teatree.core.management.commands.tasks import Command  # noqa: PLC0415

        assert "json_output" in inspect.signature(Command.list_tasks).parameters

    def test_followup_sync_declares_json_output(self) -> None:
        from teatree.core.management.commands.followup import Command  # noqa: PLC0415

        assert "json_output" in inspect.signature(Command.sync).parameters

    def test_worktree_status_and_diagnose_declare_json_output(self) -> None:
        from teatree.core.management.commands.worktree import Command  # noqa: PLC0415

        assert "json_output" in inspect.signature(Command.status).parameters
        assert "json_output" in inspect.signature(Command.diagnose).parameters

    def test_availability_show_declares_json_output(self) -> None:
        from teatree.core.management.commands.availability import Command  # noqa: PLC0415

        assert "json_output" in inspect.signature(Command.show).parameters

    def test_questions_list_declares_json_output(self) -> None:
        from teatree.core.management.commands.questions import Command  # noqa: PLC0415

        assert "json_output" in inspect.signature(Command.list_pending).parameters
