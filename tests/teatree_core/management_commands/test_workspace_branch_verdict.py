"""``workspace branch-verdict`` — the front door an agent can actually run (#4070).

The gate's advisory is only worth writing if it points at a door that opens. This pins
that the command exists, answers for a plain local branch with no ``Worktree`` row, and
serializes ``forge_merged`` / ``merged_with_post_merge_work`` / ``unique_shas`` in ONE
payload — so "the forge says merged" can never be read on its own as "safe to delete".
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.worktree import branch_classification
from tests._git_repo import make_git_repo, run_git


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "-m", f"add {name}")


def _verdicts(repo: Path, *branches: str) -> list[dict]:
    out = StringIO()
    call_command("workspace", "branch-verdict", *branches, "--repo", str(repo), "--json", stdout=out, stderr=StringIO())
    return json.loads(out.getvalue())


class BranchVerdictCommandCase(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _clone_with_origin(self) -> Path:
        origin = make_git_repo(self.root / "origin", bare=True)
        work = make_git_repo(self.root / "work")
        run_git(work, "config", "user.name", "Test")
        run_git(work, "config", "user.email", "test@example.com")
        _commit(work, "README.md", "base\n")
        run_git(work, "remote", "add", "origin", str(origin))
        run_git(work, "push", "-q", "origin", "main")
        run_git(work, "fetch", "-q", "origin")
        return work

    def _repo_with_squash_merged_feature(self) -> Path:
        repo = self._clone_with_origin()
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "one.py", "ONE\n")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--squash", "feature")
        run_git(repo, "commit", "-q", "-m", "feat: one (#1)")
        run_git(repo, "push", "-q", "origin", "main")
        run_git(repo, "fetch", "-q", "origin")
        return repo


class TestASquashMergedBranchAnswersLanded(BranchVerdictCommandCase):
    def test_json_names_the_deciding_layer(self) -> None:
        [verdict] = _verdicts(self._repo_with_squash_merged_feature(), "feature")

        assert verdict["redundant"] is True
        assert verdict["branch"] == "feature"
        assert verdict["target"] == "origin/main"
        assert verdict["source"] != "not-redundant"


class TestMergedNeverStandsAlone(BranchVerdictCommandCase):
    def test_post_merge_work_ships_in_the_same_payload(self) -> None:
        repo = self._repo_with_squash_merged_feature()
        run_git(repo, "checkout", "-q", "feature")
        _commit(repo, "later.py", "AFTER\n")

        with patch.object(branch_classification, "_branch_pr_is_merged", return_value=True):
            [verdict] = _verdicts(repo, "feature")

        assert verdict["forge_merged"] is True
        assert verdict["merged_with_post_merge_work"] is True
        assert verdict["unique_shas"]
        assert verdict["redundant"] is False


class TestTheSweepIsOneCommand(BranchVerdictCommandCase):
    def test_several_branches_answer_in_one_call(self) -> None:
        repo = self._clone_with_origin()
        for name in ("alpha", "beta"):
            run_git(repo, "checkout", "-q", "-b", name, "main")
            _commit(repo, f"{name}.py", f"{name}\n")

        verdicts = _verdicts(repo, "alpha", "beta")

        assert [v["branch"] for v in verdicts] == ["alpha", "beta"]
        assert [v["redundant"] for v in verdicts] == [False, False]


class TestTheHumanViewNamesTheVerdict(BranchVerdictCommandCase):
    def test_stderr_carries_the_branch_and_its_source(self) -> None:
        repo = self._clone_with_origin()
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "one.py", "ONE\n")
        err = StringIO()

        call_command("workspace", "branch-verdict", "feature", "--repo", str(repo), stdout=StringIO(), stderr=err)

        rendered = err.getvalue()
        assert "feature" in rendered
        assert "origin/main" in rendered
        assert "not-redundant" in rendered
