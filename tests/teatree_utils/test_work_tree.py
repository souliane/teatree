"""The work tree a check hook's staged names actually belong to.

Three venues, one contract. A plain clone is the identity case. A project
VENDORED inside a fork gets its names back prefixed, because git reports staged
paths from the work-tree top. And a hook fired from a LINKED WORKTREE gets
``GIT_DIR`` exported, which makes git call the current directory the top of the
work tree — so ``rev-parse --show-toplevel`` and the staged name list stop
agreeing, and a hook that joins them addresses a path that exists nowhere.

Every test builds a real repository under ``tmp_path`` and drives the real git,
because the whole defect lives in what git answers rather than in any parsing.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.utils import work_tree


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)  # noqa: S607 — `git` from PATH deliberately: the fixture must drive the same git the hook under test resolves
    return result.stdout


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    """A clone whose root IS the project root."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


@pytest.fixture
def vendored(plain: Path) -> Path:
    """A fork work tree carrying the project under ``vendor/core/``."""
    (plain / "vendor" / "core" / "src").mkdir(parents=True)
    (plain / "overlay").mkdir()
    return plain / "vendor" / "core"


def _stage(repo: Path, rel: str, text: str = "x = 1\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)


class TestPrefix:
    def test_plain_clone_has_no_prefix(self, plain: Path) -> None:
        assert work_tree.resolve(plain).prefix == ""

    def test_vendored_project_reports_its_offset(self, vendored: Path) -> None:
        assert work_tree.resolve(vendored).prefix == "vendor/core/"

    def test_linked_worktree_git_dir_does_not_move_the_top(self, vendored: Path, monkeypatch) -> None:
        monkeypatch.setenv("GIT_DIR", str(vendored.parents[1] / ".git"))
        assert work_tree.resolve(vendored).prefix == "vendor/core/"

    def test_untracked_directory_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(work_tree.WorkTreeError):
            work_tree.resolve(tmp_path / "nowhere")


class TestStagedNames:
    def test_names_are_project_relative_and_exclude_the_rest_of_the_fork(self, vendored: Path) -> None:
        fork = vendored.parents[1]
        _stage(fork, "vendor/core/src/mine.py")
        _stage(fork, "overlay/theirs.py")

        assert work_tree.resolve(vendored).staged_names() == ["src/mine.py"]

    def test_names_survive_the_linked_worktree_git_dir(self, vendored: Path, monkeypatch) -> None:
        fork = vendored.parents[1]
        _stage(fork, "vendor/core/src/mine.py")
        _stage(fork, "overlay/theirs.py")
        monkeypatch.setenv("GIT_DIR", str(fork / ".git"))

        assert work_tree.resolve(vendored).staged_names() == ["src/mine.py"]

    def test_plain_clone_is_unchanged(self, plain: Path) -> None:
        _stage(plain, "src/mine.py")
        assert work_tree.resolve(plain).staged_names() == ["src/mine.py"]


class TestStagedDiff:
    def test_headers_are_project_relative(self, vendored: Path) -> None:
        fork = vendored.parents[1]
        _stage(fork, "vendor/core/src/mine.py")

        diff = work_tree.resolve(vendored).staged_diff("--diff-filter=ACMR", "-U0")
        assert "+++ b/src/mine.py" in diff
        assert "vendor/core" not in diff

    def test_pathspec_resolves_against_the_project(self, vendored: Path, monkeypatch) -> None:
        fork = vendored.parents[1]
        _stage(fork, "vendor/core/pyproject.toml", '[tool.probe]\nper-file-ignores = "x"\n')
        _stage(fork, "overlay/pyproject.toml", "[tool.other]\n")
        monkeypatch.setenv("GIT_DIR", str(fork / ".git"))

        diff = work_tree.resolve(vendored).staged_diff("--diff-filter=ACMR", "-U0", "--", "pyproject.toml")
        assert "per-file-ignores" in diff
        assert "tool.other" not in diff


class TestBlobReads:
    def test_index_blob_is_found_under_the_vendoring_prefix(self, vendored: Path, monkeypatch) -> None:
        fork = vendored.parents[1]
        _stage(fork, "vendor/core/src/mine.py", "STAGED = 1\n")
        (vendored / "src" / "mine.py").write_text("UNSTAGED = 2\n", encoding="utf-8")
        monkeypatch.setenv("GIT_DIR", str(fork / ".git"))

        assert work_tree.resolve(vendored).staged_text("src/mine.py") == "STAGED = 1\n"

    def test_absent_blob_fails_loud_rather_than_reporting_nothing_to_scan(self, vendored: Path) -> None:
        with pytest.raises(work_tree.WorkTreeError):
            work_tree.resolve(vendored).staged_text("src/never_staged.py")

    def test_working_tree_read_fails_loud_on_a_path_that_does_not_resolve(self, vendored: Path) -> None:
        with pytest.raises(work_tree.WorkTreeError):
            work_tree.resolve(vendored).read("src/gone.py")

    def test_tracked_name_is_what_git_recorded(self, vendored: Path) -> None:
        assert work_tree.resolve(vendored).tracked("src/mine.py") == "vendor/core/src/mine.py"


class TestCwdAnchor:
    """The anchor a PORTABLE hook uses, and the memo behind it."""

    def test_it_resolves_the_project_the_cwd_sits_in(self, vendored: Path, monkeypatch) -> None:
        monkeypatch.chdir(vendored)
        assert work_tree.for_cwd().prefix == "vendor/core/"

    def test_the_memo_answers_a_second_call_without_re_asking_git(self, vendored: Path, monkeypatch) -> None:
        monkeypatch.chdir(vendored)
        assert work_tree.for_cwd() is work_tree.for_cwd()

    def test_the_reset_makes_the_next_call_re_resolve(self, vendored: Path, monkeypatch) -> None:
        # The efficacy half of the reset roster's entry for this cache: a test process
        # creates and destroys repositories between "runs", so a memo it cannot clear
        # answers a later test about a tree that has since changed.
        monkeypatch.chdir(vendored)
        first = work_tree.for_cwd()
        work_tree.reset_cwd_cache()
        assert work_tree.for_cwd() is not first


class TestCleanEnv:
    def test_drops_the_two_work_tree_overrides(self, monkeypatch) -> None:
        monkeypatch.setenv("GIT_DIR", "/somewhere/.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/somewhere")
        env = work_tree.clean_env()
        assert "GIT_DIR" not in env
        assert "GIT_WORK_TREE" not in env

    def test_keeps_the_index_the_commit_is_being_built_in(self, monkeypatch) -> None:
        # A partial commit (``git commit --only``) points GIT_INDEX_FILE at a
        # temporary index holding exactly the paths being committed; dropping it
        # would make every gate read a different commit than the one in flight.
        monkeypatch.setenv("GIT_INDEX_FILE", "/somewhere/.git/next-index")
        assert work_tree.clean_env()["GIT_INDEX_FILE"] == "/somewhere/.git/next-index"
