"""The GitHub lineage reader decides patch identity from real diffs, with ``gh`` answered by a real git repo (#5450)."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.merge.head_carry_over import PatchLineage
from teatree.loop.scanners.head_lineage import GhLineageReader, head_is_carried
from tests._git_repo import make_git_repo, run_git

SLUG = "souliane/teatree"
PR_ID = 5341
_LINES = [f"line {n}" for n in range(1, 21)]
_DIFF_TOO_LARGE = "gh: Sorry, the diff exceeded the maximum number of lines (20000) (HTTP 406)"


def _write(repo: Path, name: str, lines: list[str]) -> None:
    (repo / name).write_text("\n".join(lines) + "\n")


def _commit(repo: Path, message: str) -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def _edit(repo: Path, name: str, line_number: int, text: str) -> None:
    lines = (repo / name).read_text().splitlines()
    lines[line_number - 1] = text
    _write(repo, name, lines)


@dataclass(slots=True)
class _GitBackedGh:
    """The ``gh api`` surface the reader uses, answered from the repository's own git objects."""

    repo: Path
    reported_head: str
    history_cap: int | None = None
    diff_failure: str = ""
    rc: int = 0

    def __call__(self, argv: list[str], slug: str) -> "subprocess.CompletedProcess[str]":
        assert slug == SLUG
        if self.rc:
            return subprocess.CompletedProcess(argv, self.rc, "", "gh: HTTP 502")
        endpoint = argv[-1].split("?", maxsplit=1)[0]
        if endpoint.endswith(f"/pulls/{PR_ID}"):
            return self._ok(json.dumps({"base": {"ref": "main"}, "head": {"sha": self.reported_head}}))
        base, _, head = endpoint.rsplit("/compare/", maxsplit=1)[1].partition("...")
        if "Accept: application/vnd.github.diff" in argv:
            return self._diff(base, head, argv)
        return self._ok(self._commits(base, head))

    def _ok(self, stdout: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess([], 0, stdout, "")

    def _commits(self, base: str, head: str) -> str:
        rows = run_git(self.repo, "log", "--format=%H %P", f"{base}..{head}").splitlines()
        commits = [{"sha": row.split()[0], "parents": [{"sha": parent} for parent in row.split()[1:]]} for row in rows]
        total = len(commits) if self.history_cap is None else len(commits) + self.history_cap
        return json.dumps({"total_commits": total, "commits": commits})

    def _diff(self, base: str, head: str, argv: list[str]) -> "subprocess.CompletedProcess[str]":
        if self.diff_failure:
            return subprocess.CompletedProcess(argv, 1, "", self.diff_failure)
        return self._ok(run_git(self.repo, "diff", f"{base}...{head}") + "\n")


@dataclass(slots=True)
class _Scenario:
    repo: Path
    old: str

    def read(
        self, new: str, *, reported_head: str = "", history_cap: int | None = None, diff_failure: str = "", rc: int = 0
    ) -> PatchLineage | None:
        stub = _GitBackedGh(
            self.repo, reported_head=reported_head or new, history_cap=history_cap, diff_failure=diff_failure, rc=rc
        )
        return GhLineageReader(gh=stub, repo=self.repo)(
            slug=SLUG, pr_id=PR_ID, host_kind="github", old_head=self.old, new_head=new
        )


@pytest.fixture
def scenario(tmp_path: Path) -> _Scenario:
    repo = make_git_repo(tmp_path / "repo")
    _write(repo, "a.txt", _LINES)
    _write(repo, "b.txt", _LINES)
    _commit(repo, "base")
    run_git(repo, "checkout", "-q", "-b", "pr")
    _edit(repo, "a.txt", 2, "pr edit")
    old = _commit(repo, "pr change")
    run_git(repo, "checkout", "-q", "main")
    return _Scenario(repo=repo, old=old)


def _main_edits(scenario: _Scenario, name: str, line_number: int) -> None:
    run_git(scenario.repo, "checkout", "-q", "main")
    _edit(scenario.repo, name, line_number, f"main edit of {name}")
    _commit(scenario.repo, "main moves")


def _merge_main_into_pr(scenario: _Scenario) -> str:
    run_git(scenario.repo, "checkout", "-q", "pr")
    run_git(scenario.repo, "merge", "-q", "--no-edit", "main")
    return run_git(scenario.repo, "rev-parse", "HEAD")


class TestGhLineageReader:
    def test_a_manual_merge_cannot_relocate_a_reviewed_hunk_between_identical_blocks(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        block = ["same"] * 7
        _write(repo, "a.txt", block + ["separator"] * 10 + block)
        _commit(repo, "base")
        run_git(repo, "checkout", "-q", "-b", "pr")
        _edit(repo, "a.txt", 4, "danger()")
        old = _commit(repo, "reviewed patch")
        run_git(repo, "checkout", "-q", "main")
        _write(repo, "b.txt", ["new main"])
        _commit(repo, "main update")
        run_git(repo, "checkout", "-q", "pr")
        run_git(repo, "merge", "--no-commit", "--no-ff", "main")
        _edit(repo, "a.txt", 4, "same")
        _edit(repo, "a.txt", 21, "danger()")
        new = _commit(repo, "manual merge")

        lineage = _Scenario(repo, old).read(new)

        assert lineage is not None
        assert lineage.identical
        assert lineage.refusal_reason == "merge_tree_differs"

    @pytest.mark.parametrize("edit", ["    pr edit", "pr edit  "])
    def test_whitespace_in_a_reviewed_hunk_changes_its_patch_id(self, scenario: _Scenario, edit: str) -> None:
        _main_edits(scenario, "b.txt", 10)
        run_git(scenario.repo, "checkout", "-q", "pr")
        run_git(scenario.repo, "merge", "--no-commit", "--no-ff", "main")
        _edit(scenario.repo, "a.txt", 2, edit)
        new = _commit(scenario.repo, "manual whitespace change")

        lineage = scenario.read(new)

        assert lineage is not None
        assert lineage.old_patch_id != lineage.new_patch_id
        assert lineage.refusal_reason

    @pytest.mark.parametrize("change", ["mode", "rename", "symlink"])
    def test_a_manual_merge_that_changes_file_identity_is_refused(self, scenario: _Scenario, change: str) -> None:
        _main_edits(scenario, "b.txt", 10)
        run_git(scenario.repo, "checkout", "-q", "pr")
        run_git(scenario.repo, "merge", "--no-commit", "--no-ff", "main")
        source = scenario.repo / "a.txt"
        if change == "mode":
            source.chmod(0o755)
        elif change == "rename":
            source.rename(scenario.repo / "renamed.txt")
        else:
            source.unlink()
            source.symlink_to("b.txt")
        new = _commit(scenario.repo, f"manual {change} change")

        lineage = scenario.read(new)

        assert lineage is not None
        assert lineage.refusal_reason

    def test_a_pure_merge_of_main_carries_an_identical_patch(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        lineage = scenario.read(new)

        assert lineage is not None
        assert (lineage.descends, lineage.added_all_merges, lineage.refusal) == (True, True, "")
        assert lineage.old_patch_id
        assert lineage.identical
        assert lineage.refusal_reason == ""

    def test_the_matching_local_clone_proves_the_automatic_merge(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)
        gh = _GitBackedGh(scenario.repo, reported_head=new)

        with (
            patch("teatree.loop.scanners.head_lineage.find_project_root", return_value=scenario.repo),
            patch("teatree.loop.scanners.head_lineage.unambiguous_clone_path", return_value=None),
            patch("teatree.loop.scanners.head_lineage.git.remote_slug", return_value=SLUG),
        ):
            lineage = GhLineageReader(gh=gh)(
                slug=SLUG, pr_id=PR_ID, host_kind="github", old_head=scenario.old, new_head=new
            )

        assert lineage is not None
        assert lineage.refusal_reason == ""

    def test_a_clone_that_lacks_the_new_head_fetches_it_to_prove_the_merge(
        self, scenario: _Scenario, tmp_path: Path
    ) -> None:
        _main_edits(scenario, "b.txt", 10)
        clone = tmp_path / "clone"
        run_git(tmp_path, "clone", "-q", str(scenario.repo), str(clone))
        new = _merge_main_into_pr(scenario)

        lineage = GhLineageReader(gh=_GitBackedGh(scenario.repo, reported_head=new), repo=clone)(
            slug=SLUG, pr_id=PR_ID, host_kind="github", old_head=scenario.old, new_head=new
        )

        assert lineage is not None
        assert lineage.refusal_reason == ""

    def test_an_unavailable_local_clone_cannot_prove_the_merge(self, scenario: _Scenario, tmp_path: Path) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)
        gh = _GitBackedGh(scenario.repo, reported_head=new)

        lineage = GhLineageReader(gh=gh, repo=tmp_path / "missing")(
            slug=SLUG, pr_id=PR_ID, host_kind="github", old_head=scenario.old, new_head=new
        )

        assert lineage is not None
        assert lineage.identical
        assert lineage.refusal_reason == "merge_tree_unavailable"

    def test_a_repository_with_no_local_clone_cannot_prove_the_merge(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        with (
            patch("teatree.loop.scanners.head_lineage.find_project_root", return_value=None),
            patch("teatree.loop.scanners.head_lineage.unambiguous_clone_path", return_value=None),
        ):
            lineage = GhLineageReader(gh=_GitBackedGh(scenario.repo, reported_head=new))(
                slug=SLUG, pr_id=PR_ID, host_kind="github", old_head=scenario.old, new_head=new
            )

        assert lineage is not None
        assert lineage.identical
        assert lineage.refusal_reason == "merge_tree_unavailable"

    def test_main_editing_text_next_to_a_hunk_of_a_file_the_pr_changes_differs(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "a.txt", 5)
        new = _merge_main_into_pr(scenario)

        lineage = scenario.read(new)

        assert lineage is not None
        assert lineage.descends
        assert lineage.added_all_merges
        assert not lineage.identical
        assert lineage.refusal_reason == "patch_differs"

    def test_a_fix_commit_on_top_of_the_merge_is_not_a_carry(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        _merge_main_into_pr(scenario)
        _edit(scenario.repo, "a.txt", 12, "second pr edit")
        new = _commit(scenario.repo, "pr fix")

        lineage = scenario.read(new)

        assert lineage is not None
        assert not lineage.added_all_merges
        assert not lineage.identical
        assert lineage.refusal_reason == "added_commits_not_merges"

    def test_a_fix_push_is_refused_before_downloading_diffs(self, scenario: _Scenario) -> None:
        run_git(scenario.repo, "checkout", "-q", "pr")
        _edit(scenario.repo, "a.txt", 12, "second pr edit")
        new = _commit(scenario.repo, "pr fix")
        gh = _GitBackedGh(scenario.repo, reported_head=new)
        calls: list[list[str]] = []

        def counted(argv: list[str], slug: str) -> "subprocess.CompletedProcess[str]":
            calls.append(argv)
            return gh(argv, slug)

        lineage = GhLineageReader(gh=counted)(
            slug=SLUG, pr_id=PR_ID, host_kind="github", old_head=scenario.old, new_head=new
        )

        assert lineage is not None
        assert lineage.refusal_reason == "added_commits_not_merges"
        assert not any("application/vnd.github.diff" in " ".join(argv) for argv in calls)

    def test_a_non_merge_commit_that_nets_to_nothing_is_still_not_a_merge(self, scenario: _Scenario) -> None:
        run_git(scenario.repo, "checkout", "-q", "pr")
        _edit(scenario.repo, "b.txt", 4, "temporary")
        _commit(scenario.repo, "add")
        run_git(scenario.repo, "revert", "--no-edit", "HEAD")
        new = run_git(scenario.repo, "rev-parse", "HEAD")

        lineage = scenario.read(new)

        assert lineage is not None
        assert lineage.refusal_reason == "added_commits_not_merges"

    def test_a_rewritten_history_does_not_descend(self, scenario: _Scenario) -> None:
        run_git(scenario.repo, "checkout", "-q", "pr")
        run_git(scenario.repo, "commit", "-q", "--amend", "-m", "reworded")
        new = run_git(scenario.repo, "rev-parse", "HEAD")

        lineage = scenario.read(new)

        assert lineage is not None
        assert not lineage.descends
        assert lineage.refusal_reason == "not_descendant"

    def test_a_binary_change_is_refused_because_its_patch_id_ignores_the_bytes(self, scenario: _Scenario) -> None:
        run_git(scenario.repo, "checkout", "-q", "pr")
        (scenario.repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        old = _commit(scenario.repo, "binary")
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        lineage = _Scenario(repo=scenario.repo, old=old).read(new)

        assert lineage is not None
        assert lineage.refusal == "diff_unavailable"

    def test_a_diff_the_forge_cannot_render_is_a_refusal_not_a_retry(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        lineage = scenario.read(new, diff_failure=_DIFF_TOO_LARGE)

        assert lineage is not None
        assert lineage.refusal == "diff_unavailable"

    def test_a_history_the_forge_truncates_is_a_refusal(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        lineage = scenario.read(new, history_cap=1)

        assert lineage is not None
        assert lineage.refusal == "history_truncated"

    def test_a_head_that_moved_again_is_unreadable_not_a_verdict(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        assert scenario.read(new, reported_head="f" * 40) is None

    def test_a_forge_error_is_unreadable_not_a_verdict(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        assert scenario.read(new, rc=1) is None

    def test_a_missing_old_head_is_a_durable_refusal(self, scenario: _Scenario) -> None:
        _main_edits(scenario, "b.txt", 10)
        new = _merge_main_into_pr(scenario)

        lineage = scenario.read(new, diff_failure="gh: HTTP 404")

        assert lineage is not None
        assert lineage.refusal == "head_unavailable"

    def test_a_host_other_than_github_is_not_read(self, scenario: _Scenario) -> None:
        reader = GhLineageReader(gh=_GitBackedGh(scenario.repo, reported_head=scenario.old))

        with pytest.raises(ValueError, match="github"):
            reader(slug=SLUG, pr_id=PR_ID, host_kind="gitlab", old_head=scenario.old, new_head=scenario.old)


def test_a_carry_exception_leaves_the_head_unreviewed() -> None:
    with patch("teatree.loop.scanners.head_lineage.carry_over_head", side_effect=RuntimeError("probe failed")):
        assert not head_is_carried(
            slug=SLUG,
            pr_id=PR_ID,
            head_sha="a" * 40,
            host_kind="github",
            reader=GhLineageReader(),
        )
