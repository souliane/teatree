"""``t3 <overlay> session prepare-stop`` CLI (souliane/teatree#2564, PR-20)."""

import json
import os
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],  # noqa: S607 — git on PATH; fixed test argv
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestPrepareStopCommand(TestCase):
    """The command reads the control DB (open PRs, deferred questions)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(os.chdir, Path.cwd())
        env = patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(self.tmp / "state"), "CLAUDE_SESSION_ID": "sess-cli"},
        )
        env.start()
        self.addCleanup(env.stop)
        todos = patch("teatree.core.harness_todos.read_harness_todos", lambda _sid: [])
        todos.start()
        self.addCleanup(todos.stop)

    def test_json_reports_artifact_paths(self) -> None:
        work = self.tmp / "work"
        work.mkdir()
        os.chdir(work)  # non-git cwd → no at-risk worktree
        out = StringIO()
        call_command("session", "prepare-stop", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        assert payload["session_id"] == "sess-cli"
        assert Path(payload["resume_plan_path"]).exists()
        assert Path(payload["todos_path"]).exists()
        assert payload["at_risk"] == []

    def test_human_output_names_the_paths(self) -> None:
        work = self.tmp / "work2"
        work.mkdir()
        os.chdir(work)
        out = StringIO()
        call_command("session", "prepare-stop", stdout=out)
        text = out.getvalue()
        assert "resume plan:" in text
        assert "at-risk worktrees: none" in text

    def test_idempotent_rerun_captures_single_at_risk_ref(self) -> None:
        repo = self.tmp / "wt"
        repo.mkdir()
        _git(repo, "init", "-b", "feat")
        _git(repo, "config", "user.email", "t@e.st")  # privacy-scan:allow
        _git(repo, "config", "user.name", "T")
        (repo / "a.txt").write_text("base\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "init")
        (repo / "a.txt").write_text("dirty\n")  # uncommitted on an unpushed branch
        os.chdir(repo)

        call_command("session", "prepare-stop", stdout=StringIO())
        call_command("session", "prepare-stop", stdout=StringIO())  # re-run: no duplicate

        refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/t3-resume/").splitlines()
        assert len(refs) == 1
