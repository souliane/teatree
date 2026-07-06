"""``t3 <overlay> repro`` CLI over a REAL git repo (#118).

The harness-recorded seam: the agent supplies only the command string; the CLI
stamps ``git rev-parse HEAD``, runs the command, and computes ``git merge-base
--is-ancestor``. This suite drives ``call_command`` against a real repo under
``tmp_path`` so the exit-code capture and the ancestry check are exercised on
real git, not mocked into always-true — the divergent-history case proves the
ancestry gate is genuinely wired (same passing command, but the green tree is
not a descendant of the red tree, so record-green is refused).
"""

import os
import subprocess
from pathlib import Path
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.repro import Command, ReproRecordResult, ReproWaiveResult
from teatree.core.models import ReproEvidence, ReproWaiver, Ticket, Worktree

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
# The repro command: passes (exit 0) only once marker.txt says "fixed".
_CMD = "grep -q fixed marker.txt"

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],  # noqa: S607 — git resolved from PATH in test
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path, marker: str = "broken") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "marker.txt").write_text(f"{marker}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "broken state")
    return repo


class TestReproCommandWiring(TestCase):
    def test_command_exposes_the_four_subcommands(self) -> None:
        for name in ("record_red", "record_green", "waive", "status"):
            assert callable(getattr(Command, name))

    def test_result_types_carry_the_reported_keys(self) -> None:
        assert {"recorded", "provenance_ok"} <= set(ReproRecordResult.__annotations__)
        assert {"waived", "approver_id"} <= set(ReproWaiveResult.__annotations__)


class TestReproCliRealGit(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="t3-teatree", kind=Ticket.Kind.FIX)

    def _run(self, sub: str, *flags: str) -> dict[str, object]:
        return cast("dict[str, object]", call_command("repro", sub, str(self.ticket.pk), *flags))

    def test_genuine_red_then_green_with_proper_ancestry_passes(self) -> None:
        repo = _init_repo(self._tmp())
        red = self._run("record-red", "--command", _CMD, "--cwd", str(repo))
        assert red["recorded"] is True

        # Commit the fix on top of the RED tree (a proper descendant).
        (repo / "marker.txt").write_text("fixed\n", encoding="utf-8")
        _git(repo, "commit", "-aq", "-m", "fix")

        green = self._run("record-green", "--command", _CMD, "--cwd", str(repo))
        assert green["recorded"] is True
        assert green["provenance_ok"] is True
        assert ReproEvidence.objects.has_valid_repro(self.ticket) is True

    def test_fabricated_red_that_passes_is_refused(self) -> None:
        repo = _init_repo(self._tmp(), marker="fixed")  # command exits 0 -> not a failing repro
        result = self._run("record-red", "--command", _CMD, "--cwd", str(repo))
        assert result["recorded"] is False
        assert "exited 0" in cast("str", result["error"])
        assert not ReproEvidence.objects.filter(ticket=self.ticket).exists()

    def test_green_on_a_divergent_tree_is_refused_by_real_ancestry(self) -> None:
        repo = _init_repo(self._tmp())
        base_sha = _git(repo, "rev-parse", "HEAD~1")
        self._run("record-red", "--command", _CMD, "--cwd", str(repo))

        # A SIBLING commit off the base: it fixes the marker but is NOT a
        # descendant of the RED commit, so merge-base --is-ancestor is False.
        _git(repo, "checkout", "-q", "-b", "sibling", base_sha)
        (repo / "marker.txt").write_text("fixed\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "sibling fix")

        result = self._run("record-green", "--command", _CMD, "--cwd", str(repo))
        assert result["recorded"] is False
        assert "ancestor" in cast("str", result["error"])
        assert ReproEvidence.objects.has_valid_repro(self.ticket) is False

    def test_record_red_falls_back_to_the_dispatch_worktree(self) -> None:
        # No --cwd: _resolve_cwd resolves the ticket's dispatch worktree.
        repo = _init_repo(self._tmp())
        Worktree.objects.create(ticket=self.ticket, repo_path=str(repo), extra={"worktree_path": str(repo)})
        result = self._run("record-red", "--command", _CMD)
        assert result["recorded"] is True

    def test_waive_records_a_human_waiver(self) -> None:
        result = self._run("waive", "--approver", "souliane", "--reason", "hardware-timing race")
        assert result["waived"] is True
        assert ReproWaiver.objects.filter(ticket=self.ticket).exists()

    def test_waive_refuses_a_maker_approver(self) -> None:
        result = self._run("waive", "--approver", "coding-agent", "--reason", "race")
        assert result["waived"] is False
        assert not ReproWaiver.objects.filter(ticket=self.ticket).exists()

    def test_status_reports_evidence_and_waiver_state(self) -> None:
        repo = _init_repo(self._tmp())
        self._run("record-red", "--command", _CMD, "--cwd", str(repo))
        ReproWaiver.record(ticket=self.ticket, approver_id="souliane", reason="race")
        output = cast("str", call_command("repro", "status", str(self.ticket.pk)))
        assert "WAIVER by souliane" in output
        assert "gate-satisfied: True" in output

    def test_status_with_no_evidence(self) -> None:
        output = cast("str", call_command("repro", "status", str(self.ticket.pk)))
        assert "(no repro evidence, no waiver)" in output

    def _tmp(self) -> Path:
        return self._tmp_path

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
