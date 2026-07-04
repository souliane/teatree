"""PR-30 machine-interface contract, end to end.

Exercised through the django-typer CLI serialization path (``call_command``
under ``redirect_stdout``/``redirect_stderr``, which reproduces the real
``t3 ... --json`` stdout/stderr split a front-end sees). The seam's own unit
behaviour is in ``tests/teatree_core/test_machine_output.py``; these are the
end-to-end command assertions: pure-JSON on stdout under ``--json``, zero human
bytes on stdout, human diagnostics on stderr, and the ``workspace emit``
single-emit (no "Extra data") regression.
"""

import contextlib
import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase


def _run(*argv: str) -> tuple[str, str, object]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rv = call_command(*argv)
    return out.getvalue(), err.getvalue(), rv


class TestWorkspaceEmitSingleEmit(TestCase):
    """#2763 double-emit: ``workspace emit`` printed its JSON array twice.

    ``json.loads`` failed with 'Extra data' at the midpoint of the handoff.
    """

    def test_stdout_is_a_single_parseable_json_document(self) -> None:
        payload = '[{"path": "/w/a", "kind": "worktree"}]'
        with patch(
            "teatree.core.management.commands.workspace.emit_records_json",
            return_value=payload,
        ):
            out, _err, rv = _run("workspace", "emit")
        # Single document — no "Extra data"; equals the source array exactly.
        assert json.loads(out) == [{"path": "/w/a", "kind": "worktree"}]
        assert rv == payload


class TestQueueStatusJson(TestCase):
    def test_json_stdout_is_pure_json_no_human_bytes(self) -> None:
        out, _err, rv = _run("queue", "status", "--json")
        parsed = json.loads(out)  # raises if human bytes leaked onto stdout
        assert set(parsed) == {"total", "by_status", "ready_by_task"}
        assert isinstance(rv, dict)
        assert rv["total"] == parsed["total"]

    def test_non_json_leaves_stdout_empty_and_human_on_stderr(self) -> None:
        out, err, _rv = _run("queue", "status")
        assert out == ""
        assert "Total queued rows:" in err


class TestTasksJson(TestCase):
    def test_list_json_stdout_is_pure_json_array(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        call_command("tasks", "create", ticket.pk, phase="scoping", reason="Decide X.")
        out, _err, rv = _run("tasks", "list", "--json")
        parsed = json.loads(out)  # raises if the rich table leaked onto stdout
        assert isinstance(parsed, list)
        assert parsed[0]["ticket_id"] == ticket.pk
        assert rv == parsed  # call_command return mirrors the emitted JSON

    def test_list_non_json_table_goes_to_stderr_not_stdout(self) -> None:
        out, _err, _rv = _run("tasks", "list")
        assert out == ""  # the human table is on stderr, stdout stays clean

    def test_create_emits_record_json_on_stdout_confirmation_on_stderr(self) -> None:
        # tasks create is a machine handoff: the record is JSON on stdout, the
        # human confirmation on stderr (no repr'd dict interleaved, #2763).
        from teatree.core.models import Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        out, err, _rv = _run("tasks", "create", ticket.pk, "--phase", "scoping", "--reason", "Decide X.")
        parsed = json.loads(out)  # stdout is a clean JSON record
        assert parsed["ticket_id"] == ticket.pk
        assert parsed["phase"] == "scoping"
        assert "Created task" not in out  # confirmation line is not on stdout
        assert "Created task" in err
