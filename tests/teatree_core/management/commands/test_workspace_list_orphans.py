"""``workspace list-orphans --json`` is a machine channel: JSON on stdout, ``[]`` when there is none.

The session-end hook (``hooks/scripts/session_end_work_check.py``) parses it. Django's own wrapper
printed a truthy return as ``str()`` — an empty list as nothing at all and a non-empty one as a
Python repr — so neither was ever JSON, and the hook could not tell "no orphan" from "the probe
failed".
"""

import json
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

import hooks.scripts.session_end_work_check as work_check
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.machine_output import call_command_streamed
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git

_ORPHAN = BranchReport(repo="/ws/backend", branch="feat-1", status=BranchStatus.PUSHED_ORPHAN, ahead_count=3)
_FINDER = "teatree.core.management.commands._workspace.orphan_listing.find_orphans_in_workspace"


def _json_stdout(*args: str) -> str:
    stream = StringIO()
    call_command_streamed("workspace", "list-orphans", *args, "--json", stream=stream)
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


class TestListOrphansBranchFilter(TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        origin = make_git_repo(root / "origin.git", bare=True)
        clone = make_git_repo(root / "repo")
        run_git(clone, "remote", "add", "origin", str(origin))
        run_git(clone, "push", "-q", "-u", "origin", "main")
        for index, branch in enumerate(("feat-one", "feat-two"), start=1):
            run_git(clone, "switch", "-q", "-c", branch, "main")
            (clone / f"feature-{index}.txt").write_text(branch, encoding="utf-8")
            run_git(clone, "add", ".")
            run_git(clone, "commit", "-qm", f"Add {branch}")
            ticket = Ticket.objects.create(issue_url=f"https://github.com/example/repo/issues/{index}")
            Worktree.objects.create(overlay="test", ticket=ticket, repo_path="repo", branch=branch)
        run_git(clone, "switch", "-q", "main")
        self.enterContext(patch("teatree.core.gates.orphan_guard.clone_root", return_value=root))
        self.enterContext(patch("teatree.core.gates.orphan_guard.find_open_pr_for_branch", return_value=PrProbe.none()))

    def test_branch_filter_keeps_only_that_branch(self) -> None:
        entries = json.loads(_json_stdout("--branch", "feat-one"))

        assert [(entry["branch"], entry["status"]) for entry in entries] == [("feat-one", "unpushed_orphan")]

    def test_unknown_branch_is_an_empty_answer(self) -> None:
        assert json.loads(_json_stdout("--branch", "missing")) == []

    def test_without_branch_filter_the_fleet_remains_visible(self) -> None:
        assert {entry["branch"] for entry in json.loads(_json_stdout())} == {"feat-one", "feat-two"}


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
