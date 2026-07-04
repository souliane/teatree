"""PR-30 ``t3 capabilities`` — the machine-readable capability registry.

Guards that the registry is well-formed, that ``--json`` emits it as pure JSON
on stdout, and (non-vacuously) that EVERY command the registry marks ``json:true``
has a checkable JSON-emission mechanism — so the registry cannot drift from the
commands it documents.
"""

import contextlib
import inspect
import io
import json
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from typer.testing import CliRunner

import teatree.core.management.commands.workspace as workspace_mod
from teatree.cli import app
from teatree.core.capabilities import CAPABILITIES, capabilities_report


def _run(*argv: str) -> tuple[str, str, object]:
    """Invoke a management command through the real stdout/stderr split a front-end sees."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rv = call_command(*argv)
    return out.getvalue(), err.getvalue(), rv


def _switch_handler_params() -> dict[str, set[str]]:
    """Every registry command that exposes a ``--json`` / ``--format`` switch → its handler's params.

    Lazily imported: importing this module must not force a Django bootstrap.
    """
    from teatree.cli import cost as cli_cost  # noqa: PLC0415
    from teatree.cli import info as cli_info  # noqa: PLC0415
    from teatree.cli import tokens as cli_tokens  # noqa: PLC0415
    from teatree.cli.config import show as cli_config_show  # noqa: PLC0415
    from teatree.core.management.commands import (  # noqa: PLC0415
        availability,
        checking,
        do,
        env,
        followup,
        questions,
        queue,
        tasks,
        worktree,
    )

    handlers = {
        "teatree queue status": queue.Command.status,
        "teatree tasks list": tasks.Command.list_tasks,
        "teatree followup sync": followup.Command.sync,
        "teatree worktree status": worktree.Command.status,
        "teatree worktree diagnose": worktree.Command.diagnose,
        "teatree availability show": availability.Command.show,
        "teatree questions list": questions.Command.list_pending,
        "teatree checking show": checking.Command.show,
        "teatree env show": env.Command.show,
        # ``do`` is a bare-``handle`` command (no subcommand token); django-typer
        # replaces the class ``handle`` attribute with a generic wrapper, so its
        # real params live on the registered typer callback, not on ``Command.handle``.
        "teatree do": do.Command.typer_app.registered_commands[0].callback,
        "cost": cli_cost.cost,
        "tokens": cli_tokens.tokens,
        "config show": cli_config_show,
        "info artifacts": cli_info.artifacts,
    }
    return {command: set(inspect.signature(fn).parameters) for command, fn in handlers.items()}


# Switch-less always-JSON commands: no ``--json`` flag, they emit a JSON document
# unconditionally. Static signature introspection cannot tell these apart from a
# human-string command like ``workspace salvage`` (both are switch-less), so each
# is invoked and its stdout parsed in TestSwitchlessAlwaysJsonEmitsJson below.
_ALWAYS_JSON_COMMANDS = frozenset({"teatree workspace emit", "teatree tasks create", "teatree db query"})


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


class TestEveryJsonCapabilityIsProven:
    """No registry entry may claim ``json:true`` without a proof it emits JSON.

    The pre-#2954 guard only vetted the six PR-30-converted commands, so
    ``workspace salvage`` shipped ``json:true`` while returning a human line — a
    front-end's ``json.loads`` of it would crash. This ties EVERY ``json:true``
    entry to a checkable mechanism: a ``--json`` / ``--format`` switch (statically
    asserted below), or membership in ``_ALWAYS_JSON_COMMANDS`` (invoked and parsed
    in TestSwitchlessAlwaysJsonEmitsJson). A ``json:true`` entry that is neither
    fails here — exactly what would have caught the salvage over-claim.
    """

    def test_no_json_capability_is_unproven(self) -> None:
        json_caps = {cap.command for cap in CAPABILITIES if cap.json_output}
        proven = set(_switch_handler_params()) | _ALWAYS_JSON_COMMANDS
        assert json_caps == proven, (
            f"unproven json:true (add a --json/--format switch or an always-JSON invocation proof): "
            f"{sorted(json_caps - proven)}; stale proof (capability removed?): {sorted(proven - json_caps)}"
        )

    def test_switch_commands_declare_a_json_option(self) -> None:
        for command, params in _switch_handler_params().items():
            assert "json_output" in params or "output_format" in params, (
                f"{command!r} is registered json:true but its handler declares no --json/--format option"
            )


class TestSwitchlessAlwaysJsonEmitsJson(TestCase):
    """The switch-less always-JSON commands really emit parseable JSON on stdout.

    A static signature check cannot vet these (no ``--json`` flag distinguishes
    them from a human-string command), so they are the salvage-class risk: each is
    invoked here and its stdout must ``json.loads`` — the "actually emits JSON"
    half of the anti-drift guard.
    """

    def test_workspace_emit_stdout_parses(self) -> None:
        with patch.object(workspace_mod, "_worktree_root", return_value=Path("/nonexistent-ws")):
            out, _err, _rv = _run("workspace", "emit")
        assert json.loads(out) == []

    def test_tasks_create_stdout_parses(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        out, _err, _rv = _run("tasks", "create", ticket.pk, "--phase", "scoping", "--reason", "Decide X.")
        assert json.loads(out)["ticket_id"] == ticket.pk

    def test_db_query_stdout_parses(self) -> None:
        out, _err, _rv = _run("db", "query", "SELECT 1 AS n")
        assert json.loads(out) == [{"n": 1}]
