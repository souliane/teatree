"""The durations refresh must UPDATE its branch, never rebuild it (#4717).

The refresh used to `git checkout -B` the stable branch off main on every run, so a commit
already on that branch was silently dropped and `--force-with-lease` pushed the rebuild over
it. Measured on #4655: the source fixes that cleared the PR's review hold were byte-erased,
and the PR reverted to looking like nobody had ever addressed the findings.

These run real `git` against a real bare origin, because the whole defect lives in what the
ref points at afterwards — a mocked git can only re-assert the assumption under test.
"""

import subprocess
from pathlib import Path

import pytest

from scripts.ci.durations_refresh_branch import main

BRANCH = "ci/test-durations-refresh"
GENERATED = "dev/.test_durations"
HAND_FIXED = "tests/test_ceilings.py"

BASE_DURATIONS = '{"tests/a.py::t": 1.0}'
BRANCH_DURATIONS = '{"tests/a.py::t": 2.0}'
LANDED_DURATIONS = '{"tests/a.py::t": 3.0}'
FRESH_DURATIONS = '{"tests/a.py::t": 4.0}'
HAND_FIX = "TIGHT_FRACTION = 0.10\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout  # noqa: S607 — git resolves off PATH in every venue this runs


def _write(repo: Path, relative: str, content: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _refresh(repo: Path, *, fresh: str = FRESH_DURATIONS) -> int:
    """Run the branch build exactly as the workflow does: fresh durations sitting uncommitted."""
    _write(repo, GENERATED, fresh)
    return main(["--branch", BRANCH, "--repo", str(repo)])


def _advance_main(repo: Path, relative: str, content: str) -> None:
    _write(repo, relative, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"chore: main moved {relative}")
    _git(repo, "push", "-q", "origin", "main")


def _push_refresh_branch(repo: Path, *, hand_fix: str | None) -> str:
    """Publish a refresh branch, optionally carrying a hand fix, and return its tip."""
    _git(repo, "checkout", "-q", "-b", BRANCH)
    _write(repo, GENERATED, BRANCH_DURATIONS)
    _git(repo, "commit", "-qam", "chore(ci): refresh dev/.test_durations from the shard lane (#3160)")
    if hand_fix is not None:
        _write(repo, HAND_FIXED, hand_fix)
        _git(repo, "commit", "-qam", "fix(quality): drop the stale tight-band ceilings")
    tip = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "push", "-q", "origin", BRANCH)
    # CI checks out main fresh, so it never carries a local refresh branch.
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-qD", BRANCH)
    return tip


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)  # noqa: S607 — git resolves off PATH in every venue this runs
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)  # noqa: S607 — git resolves off PATH in every venue this runs
    _git(work, "config", "user.name", "refresh-bot")
    _git(work, "config", "user.email", "refresh-bot")  # deliberately not email-shaped: public-repo privacy gate
    _write(work, GENERATED, BASE_DURATIONS)
    _write(work, HAND_FIXED, "TIGHT_FRACTION = 0.75\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


@pytest.mark.integration
class TestRebuildWhenNothingWouldBeLost:
    def test_a_missing_remote_branch_is_built_from_the_base(
        self, clone: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _refresh(clone) == 0

        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == BRANCH
        assert (clone / GENERATED).read_text(encoding="utf-8") == FRESH_DURATIONS
        assert _git(clone, "rev-list", "--count", "origin/main..HEAD").strip() == "1"
        assert "mode=fresh" in capsys.readouterr().out

    def test_a_branch_carrying_only_the_generated_file_is_rebuilt(
        self, clone: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Nothing but the generated file is on it, so the rebuild that keeps the single-commit
        # history CI-3 wants discards nothing.
        _push_refresh_branch(clone, hand_fix=None)

        assert _refresh(clone) == 0

        assert _git(clone, "rev-list", "--count", "origin/main..HEAD").strip() == "1"
        assert (clone / GENERATED).read_text(encoding="utf-8") == FRESH_DURATIONS
        assert "mode=fresh" in capsys.readouterr().out


@pytest.mark.integration
class TestHandFixesSurvive:
    def test_a_commit_touching_another_file_survives_the_refresh(
        self, clone: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        hand = _push_refresh_branch(clone, hand_fix=HAND_FIX)
        _advance_main(clone, "README.md", "main moved on\n")

        assert _refresh(clone) == 0

        head = _git(clone, "rev-parse", "HEAD").strip()
        assert _is_ancestor(clone, hand, head), "the hand fix must still be reachable from the branch"
        assert _is_ancestor(clone, _git(clone, "rev-parse", "origin/main").strip(), head)
        assert (clone / HAND_FIXED).read_text(encoding="utf-8") == HAND_FIX
        assert (clone / GENERATED).read_text(encoding="utf-8") == FRESH_DURATIONS
        out = capsys.readouterr().out
        assert "mode=preserved" in out
        assert HAND_FIXED in out

    def test_a_conflict_confined_to_the_generated_file_takes_the_fresh_cassette(
        self, clone: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A previous refresh landed on main, so both sides moved the generated file. That is
        # not a decision: the freshly measured cassette is the answer by construction.
        hand = _push_refresh_branch(clone, hand_fix=HAND_FIX)
        _advance_main(clone, GENERATED, LANDED_DURATIONS)

        assert _refresh(clone) == 0

        head = _git(clone, "rev-parse", "HEAD").strip()
        assert _is_ancestor(clone, hand, head)
        assert (clone / GENERATED).read_text(encoding="utf-8") == FRESH_DURATIONS
        assert (clone / HAND_FIXED).read_text(encoding="utf-8") == HAND_FIX
        assert "mode=preserved" in capsys.readouterr().out

    def test_the_generated_file_already_matching_adds_no_empty_commit(
        self, clone: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _push_refresh_branch(clone, hand_fix=HAND_FIX)
        _advance_main(clone, "README.md", "main moved on\n")

        assert _refresh(clone, fresh=BRANCH_DURATIONS) == 0

        assert _git(clone, "log", "-1", "--format=%s").strip().startswith("Merge ")
        assert (clone / GENERATED).read_text(encoding="utf-8") == BRANCH_DURATIONS
        assert "mode=preserved" in capsys.readouterr().out


@pytest.mark.integration
class TestItRefusesRatherThanDiscard:
    def test_a_conflict_outside_the_generated_file_refuses_and_pushes_nothing(
        self, clone: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        hand = _push_refresh_branch(clone, hand_fix=HAND_FIX)
        _advance_main(clone, HAND_FIXED, "TIGHT_FRACTION = 0.50\n")

        assert _refresh(clone) == 1

        captured = capsys.readouterr()
        assert "::error::" in captured.err
        assert HAND_FIXED in captured.err
        assert not (clone / ".git" / "MERGE_HEAD").exists(), "the refused merge must be aborted"
        assert _git(clone, "rev-parse", "HEAD").strip() == hand, "the branch tip must be untouched"

    def test_a_repo_git_cannot_read_fails_loud(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path, GENERATED, FRESH_DURATIONS)

        assert main(["--branch", BRANCH, "--repo", str(tmp_path)]) == 1

        assert "::error::" in capsys.readouterr().err

    def test_a_missing_generated_file_fails_loud(self, clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (clone / GENERATED).unlink()

        assert main(["--branch", BRANCH, "--repo", str(clone)]) == 1

        assert "::error::" in capsys.readouterr().err


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],  # noqa: S607 — git resolves off PATH in every venue this runs
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
