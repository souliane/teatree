"""``workspace list-orphans --json`` is a machine channel: JSON on stdout, ``[]`` when there is none.

The session-end hook (``hooks/scripts/session_end_work_check.py``) parses it. Django's own wrapper
printed a truthy return as ``str()`` — an empty list as nothing at all and a non-empty one as a
Python repr — so neither was ever JSON, and the hook could not tell "no orphan" from "the probe
failed".
"""

import json
import subprocess
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

import hooks.scripts.session_end_work_check as work_check
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.machine_output import call_command_streamed

_ORPHAN = BranchReport(repo="/ws/backend", branch="feat-1", status=BranchStatus.PUSHED_ORPHAN, ahead_count=3)
_FINDER = "teatree.core.management.commands._workspace.helpers.find_orphans_in_workspace"


def _json_stdout() -> str:
    stream = StringIO()
    call_command_streamed("workspace", "list-orphans", "--json", stream=stream)
    return stream.getvalue()


class TestListOrphansSpeaksJson(TestCase):
    def test_no_orphan_is_an_empty_json_list(self) -> None:
        assert _json_stdout().strip() == "[]"

    def test_each_orphan_is_a_json_object(self) -> None:
        with patch(_FINDER, return_value=[_ORPHAN]):
            assert json.loads(_json_stdout()) == [
                {"repo": "/ws/backend", "branch": "feat-1", "status": "pushed_orphan", "ahead_count": 3}
            ]

    def test_without_json_stdout_carries_nothing_and_stderr_names_each_orphan(self) -> None:
        out, err = StringIO(), StringIO()
        with patch(_FINDER, return_value=[_ORPHAN]):
            call_command("workspace", "list-orphans", stdout=out, stderr=err)

        assert out.getvalue() == ""
        assert "/ws/backend (feat-1)" in err.getvalue()


class TestTheSessionEndHookReadsTheRealCommand(TestCase):
    """The hook parses what the command really prints, through the argv the hook really builds."""

    @staticmethod
    def _fetch() -> list[dict] | None:
        def _run_in_process(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            assert argv[:3] == ["/usr/bin/t3", "teatree", "workspace"], argv
            stream = StringIO()
            call_command_streamed(*argv[2:], stream=stream)
            return subprocess.CompletedProcess(argv, 0, stream.getvalue(), "")

        with (
            patch.object(work_check, "t3_argv", side_effect=lambda *args: ["/usr/bin/t3", *args]),
            patch.object(work_check, "run_t3", side_effect=_run_in_process),
        ):
            return work_check.fetch_orphans()

    def test_no_orphan_is_an_answer(self) -> None:
        assert self._fetch() == []

    def test_an_orphan_is_read_as_its_entry(self) -> None:
        with patch(_FINDER, return_value=[_ORPHAN]):
            assert self._fetch() == [
                {"repo": "/ws/backend", "branch": "feat-1", "status": "pushed_orphan", "ahead_count": 3}
            ]
