"""Regression: the staged-diff gates must scan a NAMED base (souliane/teatree#3899).

The defect these pin is not "the scan is noisy" — it is that mid-merge the scan
answers a different question than its caller asks. ``git diff --cached`` with no
base named compares the index to ``HEAD``, and during a merge the index holds
the merged result, so everything the incoming side contributed is presented as
this commit's work.

Every test here drives a REAL merge through real git rather than feeding a
hand-written diff to a parser: the bug lives entirely in which two commits git
is asked to compare, so a fixture diff cannot reproduce it. The naive test — one
branch, one commit, no merge — passes both before and after the fix, which is
exactly why the merge case is the one that has to be written.
"""

from pathlib import Path

import pytest

from scripts.hooks import check_gate_relaxation, check_quality_gates
from teatree.quality import diff_base
from tests._git_repo import git_identity_env, make_git_repo, run_git

_INHERITED_RELAXATION = '  "S999",  # added on main, not by this author'
_AUTHORED_RELAXATION = '  "E501",  # added while resolving'


def _commit(repo: Path, message: str) -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def merging_repo(tmp_path: Path) -> Path:
    """A repo stopped mid-merge, `main` carrying a relaxation the branch author never wrote.

    Shape (the #3899 report's shape, minimised):

        main:   pyproject.toml gains ``_INHERITED_RELAXATION``
        branch: forked BEFORE that, touches an unrelated file
        then:   ``git merge main`` on the branch, committed by nobody yet

    At that point the index contains main's relaxation, so a scan against the
    branch tip alone sees it as this commit's addition.
    """
    repo = make_git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text("[tool.ruff.lint]\nignore = [\n]\n", encoding="utf-8")
    _commit(repo, "base")

    run_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(repo, "branch work")

    run_git(repo, "checkout", "-q", "main")
    (repo / "pyproject.toml").write_text(
        f"[tool.ruff.lint]\nignore = [\n{_INHERITED_RELAXATION}\n]\n", encoding="utf-8"
    )
    _commit(repo, "main adds an ignore")

    run_git(repo, "checkout", "-q", "feature")
    # --no-commit stops exactly where the commit-msg hook would run. check=False:
    # a conflicted merge exits non-zero and that is still the state under test.
    run_git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
    return repo


@pytest.fixture
def merging_cwd(merging_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(merging_repo)
    for var, value in git_identity_env().items():
        monkeypatch.setenv(var, value)
    return merging_repo


class TestBaseResolution:
    def test_merge_incoming_is_named_only_during_a_merge(self, merging_cwd: Path) -> None:
        incoming = diff_base.merge_incoming()
        assert incoming is not None
        assert incoming.ref == "MERGE_HEAD"

    def test_merge_incoming_is_none_on_an_ordinary_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo(tmp_path / "plain")
        monkeypatch.chdir(repo)
        for var, value in git_identity_env().items():
            monkeypatch.setenv(var, value)
        assert diff_base.merge_incoming() is None
        assert diff_base.branch_tip().ref == "HEAD"

    def test_branch_tip_falls_back_to_the_empty_tree_before_the_first_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo(tmp_path / "unborn", initial_commit=False)
        monkeypatch.chdir(repo)
        for var, value in git_identity_env().items():
            monkeypatch.setenv(var, value)
        assert diff_base.branch_tip().ref == diff_base.EMPTY_TREE


class TestAuthoredFindings:
    def test_a_line_the_incoming_side_already_had_is_not_this_author_s(self, merging_cwd: Path) -> None:
        """The merge case. A naive one-branch test never reaches this."""
        found = diff_base.authored_findings(
            lambda diff: [line for line in diff.splitlines() if line.startswith("+")],
            lambda base: diff_base.staged_diff(base, "--diff-filter=ACMR", "-U0", "--", "pyproject.toml"),
        )
        assert not [line for line in found if "S999" in line], (
            "main's own relaxation was attributed to the merging author"
        )

    def test_a_line_written_while_resolving_is_still_reported(self, merging_cwd: Path) -> None:
        """The fix must not buy silence by exempting merges wholesale."""
        pyproject = merging_cwd / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace("]", f"{_AUTHORED_RELAXATION}\n]"),
            encoding="utf-8",
        )
        run_git(merging_cwd, "add", "pyproject.toml")

        found = diff_base.authored_findings(
            lambda diff: [line for line in diff.splitlines() if line.startswith("+")],
            lambda base: diff_base.staged_diff(base, "--diff-filter=ACMR", "-U0", "--", "pyproject.toml"),
        )
        assert [line for line in found if "E501" in line], (
            "the author's own relaxation was suppressed along with the inherited one"
        )
        assert not [line for line in found if "S999" in line]


class TestQualityGatesHookOnAMerge:
    def test_the_hook_does_not_block_a_merge_over_inherited_relaxations(self, merging_cwd: Path) -> None:
        """#3899 verbatim: resolving a merge of main must not be refused."""
        assert check_quality_gates.main() == 0

    def test_the_hook_still_blocks_a_relaxation_authored_during_the_merge(self, merging_cwd: Path) -> None:
        pyproject = merging_cwd / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace("]", f"{_AUTHORED_RELAXATION}\n]"),
            encoding="utf-8",
        )
        run_git(merging_cwd, "add", "pyproject.toml")
        assert check_quality_gates.main() == 1

    def test_an_ordinary_commit_is_unaffected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Outside a merge the base is still the branch tip — no behaviour change."""
        repo = make_git_repo(tmp_path / "plain")
        (repo / "pyproject.toml").write_text("[tool.ruff.lint]\nignore = [\n]\n", encoding="utf-8")
        _commit(repo, "base")
        (repo / "pyproject.toml").write_text(
            f"[tool.ruff.lint]\nignore = [\n{_AUTHORED_RELAXATION}\n]\n", encoding="utf-8"
        )
        run_git(repo, "add", "pyproject.toml")

        monkeypatch.chdir(repo)
        for var, value in git_identity_env().items():
            monkeypatch.setenv(var, value)
        assert check_quality_gates.main() == 1


class TestGateRelaxationHookOnAMerge:
    """The sibling gate #3899 names: same bare base, same misattribution.

    It survived in the field only because its ``ALLOW_GATE_RELAX`` escape lets a
    truthful reason through — an escape is not a reason to keep scanning the
    wrong changes.
    """

    def test_an_inherited_noqa_is_not_charged_to_the_merging_author(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo(tmp_path / "relax")
        (repo / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "base")

        run_git(repo, "checkout", "-q", "-b", "feature")
        (repo / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "branch work")

        run_git(repo, "checkout", "-q", "main")
        (repo / "mod.py").write_text("VALUE = 1  # noqa\n", encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "main suppresses")

        run_git(repo, "checkout", "-q", "feature")
        run_git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)

        monkeypatch.chdir(repo)
        for var, value in git_identity_env().items():
            monkeypatch.setenv(var, value)

        assert not [f.path for f in check_gate_relaxation._authored_findings()]
