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


class TestWorkspaceSalvageSingleEmit(TestCase):
    """#2954 double-emit: ``workspace salvage`` printed its human outcome twice.

    ``self.stdout.write(line)`` then ``return line`` made django-typer repr the
    return a SECOND time (the same #2763 class ``workspace emit`` fixed), so the
    outcome line printed twice.
    """

    def test_outcome_line_prints_exactly_once(self) -> None:
        import teatree.core.management.commands._workspace_salvage as ws_salvage_mod  # noqa: PLC0415
        from teatree.core.cleanup.cleanup_salvage import SalvageResult  # noqa: PLC0415

        result = SalvageResult(salvaged=True, deleted=True, pr_url="https://x/pr/9", salvage_branch="salvage/feat")
        with (
            patch.object(ws_salvage_mod.git, "run", return_value="/repo"),
            patch.object(ws_salvage_mod, "salvage_item", return_value=result),
        ):
            out, _err, rv = _run("workspace", "salvage", "feat")
        assert out.count("salvaged=True") == 1, "outcome line must print exactly once, not twice"
        assert "salvaged=True" in rv  # the return still feeds call_command consumers


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
        # The human view is a table on stderr; stdout stays a clean JSON channel.
        assert "Queue" in err
        assert "Status" in err


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


class TestStringReturnJson(TestCase):
    """String-return commands emit JSON on stdout under --json.

    availability show / questions list return a ``json.dumps`` string, preserving
    human-on-stdout otherwise — the checking.py precedent, distinct from the
    emit-seam stderr split.
    """

    def test_availability_show_json_is_parseable(self) -> None:
        out, _err, _rv = _run("availability", "show", "--json")
        parsed = json.loads(out)
        assert set(parsed) == {"mode", "source"}

    def test_questions_list_json_is_parseable_array(self) -> None:
        out, _err, _rv = _run("questions", "list", "--json")
        assert json.loads(out) == []  # empty DB → empty JSON array, not "no deferred questions."
