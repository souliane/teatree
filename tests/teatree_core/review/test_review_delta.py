from pathlib import Path

from teatree.core.review.review_delta import resolve_delta
from tests._git_repo import run_git, run_git_captured


def _git(repo: Path, *args: str) -> str:
    return run_git(repo, *args)


def test_delta_contains_only_pr_side_commits_after_prior_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature")
    (repo / "feature.txt").write_text("old\n")
    (repo / "second.txt").write_text("unchanged\n")
    (repo / "third.txt").write_text("unchanged\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "first review")
    prior = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "main-only.txt").write_text("from main only\n")
    (repo / "shared.txt").write_text("main side\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main change")
    _git(repo, "checkout", "feature")
    (repo / "feature.txt").write_text("fixed\n")
    (repo / "shared.txt").write_text("feature side\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix")
    conflict = run_git_captured(repo, "merge", "main", "--no-edit")
    assert conflict.returncode == 1
    (repo / "shared.txt").write_text("resolved both\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "resolve merge")
    head = _git(repo, "rev-parse", "HEAD")

    delta = resolve_delta(repo, [prior], head, "main", "main")

    assert delta.case == "delta"
    assert "+fixed" in delta.patch
    assert "+resolved both" in delta.patch
    assert "main-only.txt" not in delta.patch
    assert delta.files == frozenset({"feature.txt", "shared.txt"})


def test_rewritten_history_falls_back_to_full_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("head\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    delta = resolve_delta(repo, ["a" * 40], head, "main", "main")
    assert delta.case == "full"
    assert delta.reason
    assert not delta.patch


def test_full_review_fallbacks_name_the_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-b", "feature")
    for name in ("one.txt", "two.txt", "three.txt"):
        (repo / name).write_text("before\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "prior")
    prior = _git(repo, "rev-parse", "HEAD")
    for name in ("one.txt", "two.txt"):
        (repo / name).write_text("after\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "large fix")
    head = _git(repo, "rev-parse", "HEAD")

    assert resolve_delta(repo, [], head, "main", "main").reason == "no prior verdict"
    assert resolve_delta(repo, [prior], head, "main", "").reason == "prior review base is unknown"
    assert resolve_delta(repo, [prior], head, "main", "release").reason == "base changed since the prior review"
    large = resolve_delta(repo, [prior], head, "main", "main")
    assert large.case == "full"
    assert large.reason == "delta touches more than 50% of PR files"
    assert not large.patch
