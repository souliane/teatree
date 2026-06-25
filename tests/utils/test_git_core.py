from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.utils import git
from teatree.utils import run as utils_run_mod
from tests.teatree_core.cleanup._shared import _run_git


def test_default_branch_prefers_symbolic_ref_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        calls.append(args)
        if args[-1] == "refs/remotes/origin/HEAD":
            return CompletedProcess(args, 1, "", "")
        if args[-1] == "refs/remotes/origin/main":
            return CompletedProcess(args, 0, "", "")
        return CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    assert git.default_branch("/tmp/repo") == "main"
    assert calls[0][-1] == "refs/remotes/origin/HEAD"


def test_default_branch_returns_symbolic_ref_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(list(args[0]), 0, "refs/remotes/origin/main\n", ""),
    )

    assert git.default_branch("/tmp/repo") == "main"


def test_git_helpers_cover_run_check_current_branch_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        if args[-2:] == ["status", "--short"]:
            return CompletedProcess(args, 0, " M pyproject.toml\n", "")
        if args[-2:] == ["rev-parse", "--abbrev-ref"]:
            return CompletedProcess(args, 0, "", "")
        return CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    assert git.run(repo="/tmp/repo", args=["status", "--short"]) == "M pyproject.toml"
    assert git.check(repo="/tmp/repo", args=["status", "--short"]) is True
    assert git.current_branch("/tmp/repo") == ""
    with pytest.raises(RuntimeError, match="Could not detect default branch"):
        git.default_branch("/tmp/repo")


def test_run_strict_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
        **_: object,
    ) -> CompletedProcess[str]:
        if "bad" in args:
            return CompletedProcess(args, 1, "", "fatal")
        return CompletedProcess(args, 0, "ok\n", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    assert git.run(repo="/tmp/r", args=["log"]) == "ok"
    with pytest.raises(utils_run_mod.CommandFailedError):
        git.run_strict(repo="/tmp/r", args=["bad"])


def test_git_high_level_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        calls.append(list(args))
        if "merge-base" in args:
            return CompletedProcess(args, 0, "abc123\n", "")
        if "rev-list" in args:
            return CompletedProcess(args, 0, "3\n", "")
        if "log" in args:
            return CompletedProcess(args, 0, "abc feat one\ndef feat two\n", "")
        if "status" in args:
            return CompletedProcess(args, 0, " M file.py\n", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    assert git.merge_base("/tmp/r", "origin/main") == "abc123"
    assert git.rev_count("/tmp/r", "abc123..HEAD") == 3
    assert git.log_oneline("/tmp/r", "abc123..HEAD") == "abc feat one\ndef feat two"
    assert git.status_porcelain("/tmp/r") == "M file.py"

    git.soft_reset("/tmp/r", "abc123")
    assert any("reset" in c for c in calls)

    git.commit("/tmp/r", "squash msg")
    assert any("commit" in c for c in calls)

    git.fetch("/tmp/r", "origin", "main")
    assert any("fetch" in c for c in calls)

    git.rebase("/tmp/r", "origin/main")
    assert any("rebase" in c for c in calls)


def test_git_worktree_and_branch_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        if "worktree" in args:
            return CompletedProcess(args, 0, "", "")
        if "branch" in args:
            return CompletedProcess(args, 1, "", "")
        if "pull" in args:
            return CompletedProcess(args, 0, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    assert git.worktree_remove("/tmp/r", "/tmp/wt") is True
    assert git.branch_delete("/tmp/r", "old-branch") is False
    assert git.pull_ff_only("/tmp/r") is True


def test_fetch_without_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        calls.append(list(args))
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    git.fetch("/tmp/r")
    assert calls[-1] == ["git", "-C", "/tmp/r", "fetch", "origin"]


def test_remote_url_returns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *a, **kw: CompletedProcess(a[0], 0, "git@github.com:acme/repo.git\n", ""),
    )
    assert git.remote_url(repo="/tmp/r", remote="origin") == "git@github.com:acme/repo.git"


@pytest.mark.parametrize(
    ("remote_url_value", "expected_slug"),
    [
        ("git@github.com:acme/repo.git", "acme/repo"),
        ("git@github.com:acme/repo", "acme/repo"),
        ("https://github.com/acme/repo.git", "acme/repo"),
        ("https://github.com/acme/repo", "acme/repo"),
        ("ssh://git@github.com/acme/repo.git", "acme/repo"),
        ("git@gitlab.com:group/sub/proj.git", "group/sub/proj"),
        ("https://gitlab.com/group/sub/proj.git", "group/sub/proj"),
    ],
)
def test_remote_slug_parses_supported_url_forms(
    remote_url_value: str,
    expected_slug: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *a, **kw: CompletedProcess(a[0], 0, f"{remote_url_value}\n", ""),
    )
    assert git.remote_slug(repo="/tmp/r", remote="origin") == expected_slug


def test_remote_slug_returns_empty_when_no_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *a, **kw: CompletedProcess(a[0], 1, "", "no remote"),
    )
    assert git.remote_slug(repo="/tmp/r") == ""


def test_remote_slug_passes_through_when_path_is_already_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers can hand an already-resolved slug (``owner/repo``) and get it back unchanged."""
    assert git.remote_slug(repo="acme/repo") == "acme/repo"
    assert git.remote_slug(repo="group/sub/proj") == "group/sub/proj"


def test_config_value_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *a, **kw: CompletedProcess(a[0], 0, "Jane Doe\n", ""),
    )
    assert git.config_value(key="user.name") == "Jane Doe"


def test_last_commit_message_parses_subject_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *a, **kw: CompletedProcess(a[0], 0, "fix: bug\n\nDetailed body\n", ""),
    )
    subject, body = git.last_commit_message(repo="/tmp/r")
    assert subject == "fix: bug"
    assert body == "Detailed body"


def test_last_commit_message_subject_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *a, **kw: CompletedProcess(a[0], 0, "feat: add feature", ""),
    )
    subject, body = git.last_commit_message()
    assert subject == "feat: add feature"
    assert body == ""


def _origin_with_branch(tmp_path: Path, branch_commits: list[tuple[str, str]]) -> Path:
    """An ``origin/main`` clone with a ``feature`` branch carrying ``branch_commits``.

    The shared ``GIT_*``-stripped runner keeps the tmp repo isolated from the
    outer ``git commit`` hook env (#288).
    """
    origin = tmp_path / "origin.git"
    _run_git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    clone = tmp_path / "clone"
    _run_git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _run_git("config", "user.email", "t@t", cwd=clone)
    _run_git("config", "user.name", "t", cwd=clone)
    _run_git("commit", "--allow-empty", "-q", "-m", "feat(lifecycle): unrelated already-merged", cwd=clone)
    _run_git("push", "-q", "origin", "main", cwd=clone)
    _run_git("checkout", "-q", "-b", "feature", cwd=clone)
    for subject, body in branch_commits:
        message = f"{subject}\n\n{body}" if body else subject
        _run_git("commit", "--allow-empty", "-q", "-m", message, cwd=clone)
    return clone


def test_first_commit_message_returns_oldest_branch_commit_not_default_head(tmp_path: Path) -> None:
    clone = _origin_with_branch(
        tmp_path,
        [("fix(y): the real work", "Body line.\n\nSecond paragraph."), ("fix(y): a follow-up", "")],
    )
    subject, body = git.first_commit_message(repo=str(clone), range_spec="origin/main..feature")
    assert subject == "fix(y): the real work"
    assert body == "Body line.\n\nSecond paragraph."


def test_first_commit_message_empty_when_no_commits_in_range(tmp_path: Path) -> None:
    clone = _origin_with_branch(tmp_path, [("fix(y): work", "")])
    assert git.first_commit_message(repo=str(clone), range_spec="origin/main..origin/main") == ("", "")


def test_first_commit_message_empty_range_spec_yields_empty(tmp_path: Path) -> None:
    clone = _origin_with_branch(tmp_path, [("fix(y): work", "")])
    assert git.first_commit_message(repo=str(clone), range_spec="") == ("", "")


def test_worktree_add_with_and_without_create_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        calls.append(list(args))
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    assert git.worktree_add("/tmp/r", "/tmp/wt", "feat-1", create_branch=True) is True
    assert "-b" in calls[-1]

    assert git.worktree_add("/tmp/r", "/tmp/wt2", "feat-1", create_branch=False) is True
    assert "-b" not in calls[-1]
    assert "feat-1" in calls[-1]


def _orphan_ref_worktree(tmp_path: Path) -> tuple[Path, str]:
    """Build a worktree whose checked-out branch ref is deleted; return (wt_path, head_sha).

    Mirrors the production orphan state: ``refs/heads/feat-x`` is removed while
    the worktree dir survives, so the worktree HEAD is a dangling symref and the
    tip SHA lives only in the per-worktree reflog.
    """
    main = tmp_path / "main"
    main.mkdir()
    _run_git("init", "-q", "-b", "main", str(main), cwd=tmp_path)
    _run_git("config", "user.email", "t@t", cwd=main)
    _run_git("config", "user.name", "t", cwd=main)
    (main / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git("add", "-A", cwd=main)
    _run_git("commit", "-q", "-m", "initial", cwd=main)
    wt = tmp_path / "wt-featx"
    _run_git("worktree", "add", "-q", "-b", "feat-x", str(wt), cwd=main)
    _run_git("config", "user.email", "t@t", cwd=wt)
    _run_git("config", "user.name", "t", cwd=wt)
    (wt / "f.txt").write_text("work\n", encoding="utf-8")
    _run_git("add", "-A", cwd=wt)
    _run_git("commit", "-q", "-m", "feat x", cwd=wt)
    head_sha = git.head_sha(str(wt))
    _run_git("update-ref", "-d", "refs/heads/feat-x", cwd=main)
    return wt, head_sha


def test_recovered_head_sha_after_ref_gone_returns_last_head(tmp_path: Path) -> None:
    wt, head_sha = _orphan_ref_worktree(tmp_path)
    # The named ref is gone, so a normal HEAD probe would fail — but the reflog
    # still records the tip the worktree pointed at.
    assert git.recovered_head_sha_after_ref_gone(str(wt)) == head_sha


def test_recovered_head_sha_after_ref_gone_none_when_dir_gone(tmp_path: Path) -> None:
    assert git.recovered_head_sha_after_ref_gone(str(tmp_path / "missing")) is None


def test_recovered_head_sha_after_ref_gone_none_when_dir_is_not_a_git_repo(tmp_path: Path) -> None:
    # An existing dir that is NOT a git repo: ``rev-parse --absolute-git-dir``
    # yields no git dir, so recovery returns None rather than reading a stray file.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git.recovered_head_sha_after_ref_gone(str(plain)) is None


def test_recovered_head_sha_after_ref_gone_none_on_malformed_reflog(tmp_path: Path) -> None:
    wt, _ = _orphan_ref_worktree(tmp_path)
    git_dir = Path(git.run(repo=str(wt), args=["rev-parse", "--absolute-git-dir"]))
    (git_dir / "logs" / "HEAD").write_text("one-field-only\n", encoding="utf-8")
    assert git.recovered_head_sha_after_ref_gone(str(wt)) is None


def test_recovered_head_sha_after_ref_gone_none_on_empty_reflog(tmp_path: Path) -> None:
    wt, _ = _orphan_ref_worktree(tmp_path)
    git_dir = Path(git.run(repo=str(wt), args=["rev-parse", "--absolute-git-dir"]))
    (git_dir / "logs" / "HEAD").write_text("", encoding="utf-8")
    assert git.recovered_head_sha_after_ref_gone(str(wt)) is None


def test_recovered_head_sha_after_ref_gone_none_when_no_reflog_file(tmp_path: Path) -> None:
    wt, _ = _orphan_ref_worktree(tmp_path)
    git_dir = Path(git.run(repo=str(wt), args=["rev-parse", "--absolute-git-dir"]))
    (git_dir / "logs" / "HEAD").unlink()
    assert git.recovered_head_sha_after_ref_gone(str(wt)) is None


def test_recovered_head_sha_after_ref_gone_none_when_sha_unresolvable(tmp_path: Path) -> None:
    wt, _ = _orphan_ref_worktree(tmp_path)
    git_dir = Path(git.run(repo=str(wt), args=["rev-parse", "--absolute-git-dir"]))
    deadbeef = "dead" * 10  # 40 hex chars that resolve to no object
    (git_dir / "logs" / "HEAD").write_text(
        f"{'0' * 40} {deadbeef} t <t@t> 1700000000 +0000\tcommit: gone\n", encoding="utf-8"
    )
    assert git.recovered_head_sha_after_ref_gone(str(wt)) is None
