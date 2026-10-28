"""The size floors name what this branch added, measured against its merge base with main."""

from pathlib import Path

from teatree.utils.git import run_strict
from tests._budget_report import branch_skill_bytes, import_report, skill_report, skill_section_sizes


def _commit_all(repo: Path, message: str) -> None:
    run_strict(repo=str(repo), args=["add", "-A"])
    run_strict(repo=str(repo), args=["-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-m", message])


def _repo(tmp_path: Path, *, trunk: str = "main") -> Path:
    repo = tmp_path / "repo"
    (repo / "skills" / "ship").mkdir(parents=True)
    run_strict(repo=str(tmp_path), args=["init", "-b", trunk, str(repo)])
    (repo / "skills" / "ship" / "SKILL.md").write_text("a" * 100)
    _commit_all(repo, "base")
    run_strict(repo=str(repo), args=["checkout", "-b", "feature"])
    (repo / "skills" / "ship" / "SKILL.md").write_text("a" * 160)
    (repo / "src" / "teatree").mkdir(parents=True)
    (repo / "src" / "teatree" / "newmod.py").write_text("x = 1\n")
    _commit_all(repo, "feature")
    return repo


def test_skill_bytes_changed_on_the_branch_are_measured_against_the_merge_base(tmp_path: Path) -> None:
    assert branch_skill_bytes(_repo(tmp_path)) == {"skills/ship/SKILL.md": 60}


def test_a_checkout_with_no_main_reports_the_branch_delta_as_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, trunk="trunk")

    assert branch_skill_bytes(repo) is None
    assert "unknown (no merge base with main" in skill_report("--- SKILL: ship ---\nbody", repo)


def test_the_skill_report_names_the_largest_sections_and_the_branch_delta(tmp_path: Path) -> None:
    context = "--- SKILL: review ---\n" + "r" * 20 + "\n--- SKILL: ship ---\n" + "s" * 500

    assert list(skill_section_sizes(context)) == ["review", "ship"]
    report = skill_report(context, _repo(tmp_path))
    assert report.startswith("largest skill sections: ship ")
    assert "skills/ship/SKILL.md +60 B" in report


def test_the_import_report_names_the_groups_the_forbidden_package_and_the_modules_the_branch_added(
    tmp_path: Path,
) -> None:
    modules = ["teatree.core.a", "teatree.core.b", "openai", "openai.types", "teatree.newmod", "os"]

    report = import_report(modules, _repo(tmp_path), ("openai",))

    assert "teatree.core 2" in report
    assert "openai 2" in report
    assert "Forbidden packages loaded: openai." in report
    assert "load at startup: teatree.newmod." in report
