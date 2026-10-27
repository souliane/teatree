from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


def test_latest_verdict_rewritten_history_does_not_reuse_an_older_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-b", "feature")
    (repo / "file.txt").write_text("first pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "first")
    older = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "abandoned")
    (repo / "file.txt").write_text("latest held pass\n")
    _git(repo, "commit", "-am", "held")
    latest = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "feature")
    (repo / "file.txt").write_text("rewritten fix\n")
    _git(repo, "commit", "-am", "fix")
    head = _git(repo, "rev-parse", "HEAD")

    delta = resolve_delta(repo, [latest, older], head, "main", "main")

    assert delta.case == "full"
    assert delta.prior_head == latest
    assert delta.reason == "prior head is not an ancestor"


def test_one_file_pr_keeps_only_new_hunks_on_second_pass(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-b", "feature")
    (repo / "only.txt").write_text("first pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "first pass")
    prior = _git(repo, "rev-parse", "HEAD")
    (repo / "only.txt").write_text("first pass\nsecond pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "second pass")
    head = _git(repo, "rev-parse", "HEAD")

    delta = resolve_delta(repo, [prior], head, "main", "main")

    assert delta.case == "delta"
    assert "+second pass" in delta.patch
    assert "+first pass" not in delta.patch


@pytest.mark.parametrize(
    ("name", "git_config"), [("nested/touched.py", "noprefix"), ("a path with spaces.py", "default")]
)
def test_delta_tracks_changed_paths_with_git_config_and_spaces(tmp_path: Path, name: str, git_config: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-b", "feature")
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    for other in (path, repo / "second.py", repo / "third.py"):
        other.write_text("before\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "prior")
    prior = _git(repo, "rev-parse", "HEAD")
    path.write_text("after\n")
    _git(repo, "commit", "-am", "fix")
    head = _git(repo, "rev-parse", "HEAD")
    if git_config == "noprefix":
        _git(repo, "config", "diff.noprefix", "true")

    delta = resolve_delta(repo, [prior], head, "main", "main")

    assert delta.case == "delta"
    assert delta.files == frozenset({name})
    assert "+after" in delta.patch


def test_prior_commit_is_fetched_before_ancestry_check(tmp_path: Path) -> None:
    prior = "a" * 40
    head = "b" * 40
    with (
        patch("teatree.core.review.review_delta.run_with_status") as status,
        patch("teatree.core.review.review_delta.git.run_strict_verbatim") as git_read,
    ):
        status.side_effect = [
            SimpleNamespace(returncode=1),
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
        ]
        git_read.side_effect = ["diff --git a/touched.py b/touched.py\n+fixed\n", "touched.py\0other.py\0third.py\0"]

        delta = resolve_delta(tmp_path, [prior], head, "main", "main")

    assert delta.case == "delta"
    assert any(call.kwargs["args"][:3] == ["fetch", "--no-tags", "origin"] for call in status.call_args_list)


def test_git_patch_failure_names_a_full_review(tmp_path: Path) -> None:
    with (
        patch("teatree.core.review.review_delta.run_with_status", return_value=SimpleNamespace(returncode=0)),
        patch("teatree.core.review.review_delta.git.run_strict_verbatim", side_effect=OSError("git failed")),
    ):
        delta = resolve_delta(tmp_path, ["a" * 40], "b" * 40, "main", "main")

    assert delta.case == "full"
    assert delta.reason == "git could not produce the delta (OSError)"


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
    _git(repo, "config", "diff.noprefix", "true")

    assert resolve_delta(repo, [], head, "main", "main").reason == "no prior verdict"
    assert resolve_delta(repo, [head], head, "main", "main").reason == "latest verdict already covers this head"
    assert resolve_delta(repo, [prior], head, "main", "").reason == "delta touches more than 50% of PR files"
    assert resolve_delta(repo, [prior], head, "main", "release").reason == "base changed since the prior review"
    large = resolve_delta(repo, [prior], head, "main", "main")
    assert large.case == "full"
    assert large.reason == "delta touches more than 50% of PR files"
    assert not large.patch
