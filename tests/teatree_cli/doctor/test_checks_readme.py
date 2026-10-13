"""README feature drift is visible on the doctor surface."""

from pathlib import Path

import pytest

from teatree.cli.doctor import checks_readme
from teatree.cli.doctor.checks_readme import readme_feature_merges_since_update
from tests._git_repo import make_git_repo, run_git


def test_feature_merges_since_readme_update_are_counted(tmp_path: Path) -> None:
    make_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("start", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-qm", "docs: start")
    (tmp_path / "feature.txt").write_text("one", encoding="utf-8")
    run_git(tmp_path, "add", "feature.txt")
    run_git(tmp_path, "commit", "-qm", "feat: one")
    assert readme_feature_merges_since_update(tmp_path) == 1
    run_git(tmp_path, "commit", "--allow-empty", "-qm", "fix: unrelated")
    run_git(tmp_path, "commit", "--allow-empty", "-qm", "feat(cli): scoped")
    assert readme_feature_merges_since_update(tmp_path) == 2
    (tmp_path / "README.md").write_text("refreshed", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-qm", "docs: refresh")
    assert readme_feature_merges_since_update(tmp_path) == 0


def test_feature_count_requires_tracked_readme(tmp_path: Path) -> None:
    make_git_repo(tmp_path)
    run_git(tmp_path, "commit", "--allow-empty", "-qm", "feat: initial")
    with pytest.raises(ValueError, match=r"README\.md has no tracked commit"):
        readme_feature_merges_since_update(tmp_path)


def test_doctor_warns_after_five_feature_commits(tmp_path: Path, monkeypatch, capsys) -> None:
    make_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("start", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-qm", "docs: start")
    for index in range(5):
        path = tmp_path / f"feature-{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        run_git(tmp_path, "add", path.name)
        run_git(tmp_path, "commit", "-qm", f"feat: {index}")
    monkeypatch.setattr(checks_readme, "CODE_REPO_ROOT", tmp_path)
    assert checks_readme._check_readme_staleness() is False
    assert "5 feature commits" in capsys.readouterr().out


def test_doctor_reports_unreadable_history(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(checks_readme, "CODE_REPO_ROOT", tmp_path)
    assert checks_readme._check_readme_staleness() is False
    assert "could not be checked" in capsys.readouterr().out


def test_doctor_accepts_recent_readme_update(tmp_path: Path, monkeypatch) -> None:
    make_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("recent", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-qm", "docs: update")
    monkeypatch.setattr(checks_readme, "CODE_REPO_ROOT", tmp_path)
    assert checks_readme._check_readme_staleness() is True
