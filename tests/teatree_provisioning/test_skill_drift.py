"""Installed-vs-source skill comparison — the measurement behind the drift gate.

A skill install is a dereferenced physical copy, so the installed bytes and the
reviewed source can diverge with nothing reporting it. These exercise the real
thing end to end: a real git clone, real ``SKILL.md`` files on disk, no mocks —
including the two properties the copy model exists for (the answer must come from
the reviewed ref, never from whatever branch or dirty state the clone sits on).
"""

from pathlib import Path

import pytest

from teatree.provisioning.skill_drift import SkillSourceClone, measure_skill_drift
from tests._git_repo import make_git_repo, run_git


def _write_skill(root: Path, directory: str, name: str, body: str = "reviewed") -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}\n", encoding="utf-8")
    return skill_md


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """An upstream repo publishing two skills on its default branch."""
    origin = make_git_repo(tmp_path / "origin")
    _write_skill(origin, "backend-dev", "backend-dev")
    _write_skill(origin, "qa", "qa")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "publish skills")
    return origin


@pytest.fixture
def clone(tmp_path: Path, source: Path) -> Path:
    """A local clone of *source*, so ``origin/HEAD`` resolves as it does in the field."""
    run_git(tmp_path, "clone", "-q", str(source), str(tmp_path / "clone"))
    return tmp_path / "clone"


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    return tmp_path / "installed"


def _clone_config(clone: Path) -> SkillSourceClone:
    return SkillSourceClone(label="team/skills", paths=[str(clone)])


class TestMeasureSkillDrift:
    def test_identical_install_is_clean(self, clone: Path, installed: Path) -> None:
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.is_clean
        assert drift.stale == ()
        assert drift.absent == ()

    def test_installed_copy_left_behind_by_a_merged_fix_is_stale(self, clone: Path, installed: Path) -> None:
        # The incident: the source moved on, the copy did not, and nothing said so.
        _write_skill(installed, "backend-dev", "backend-dev", body="the old instructions")
        _write_skill(installed, "qa", "qa")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.stale == ("backend-dev",)
        assert drift.absent == ()

    def test_skill_the_source_publishes_but_nobody_installed_is_absent(self, clone: Path, installed: Path) -> None:
        _write_skill(installed, "backend-dev", "backend-dev")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.absent == ("qa",)
        assert drift.stale == ()

    def test_install_identity_is_the_declared_name_not_the_directory(
        self, source: Path, clone: Path, installed: Path
    ) -> None:
        # A skill living at internal/ao-<x>/ installs as <x>; keying on the
        # directory would report every such skill as never installed.
        _write_skill(source, "internal/ao-elite-review", "elite-review")
        run_git(source, "add", "-A")
        run_git(source, "commit", "-q", "-m", "add nested skill")
        run_git(clone, "fetch", "-q", "origin")
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        _write_skill(installed, "elite-review", "elite-review")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.is_clean

    def test_symlinked_skill_entry_is_not_counted_twice(self, source: Path, clone: Path, installed: Path) -> None:
        # The repo points <x>/SKILL.md at its real home; the link blob holds a
        # path, not a skill, so only the target may produce a row.
        _write_skill(source, "internal/ao-elite-review", "elite-review")
        (source / "elite-review").mkdir()
        (source / "elite-review" / "SKILL.md").symlink_to("../internal/ao-elite-review/SKILL.md")
        run_git(source, "add", "-A")
        run_git(source, "commit", "-q", "-m", "add symlinked skill")
        run_git(clone, "fetch", "-q", "origin")
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.absent == ("elite-review",)

    def test_a_stale_reference_file_makes_its_skill_stale(self, source: Path, clone: Path, installed: Path) -> None:
        # A skill installs as its whole directory, so a merged fix landing only in
        # references/ is drift a SKILL.md-only comparison cannot see.
        (source / "qa" / "references").mkdir()
        (source / "qa" / "references" / "checklist.md").write_text("the reviewed checklist\n", encoding="utf-8")
        run_git(source, "add", "-A")
        run_git(source, "commit", "-q", "-m", "publish a reference file")
        run_git(clone, "fetch", "-q", "origin")
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        (installed / "qa" / "references").mkdir()
        (installed / "qa" / "references" / "checklist.md").write_text("the old checklist\n", encoding="utf-8")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.stale == ("qa",)

    def test_a_reference_file_the_install_never_received_makes_its_skill_stale(
        self, source: Path, clone: Path, installed: Path
    ) -> None:
        (source / "qa" / "references").mkdir()
        (source / "qa" / "references" / "checklist.md").write_text("the reviewed checklist\n", encoding="utf-8")
        run_git(source, "add", "-A")
        run_git(source, "commit", "-q", "-m", "publish a reference file")
        run_git(clone, "fetch", "-q", "origin")
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.stale == ("qa",)

    def test_answer_comes_from_the_reviewed_ref_not_the_checked_out_branch(self, clone: Path, installed: Path) -> None:
        # Exactly why the install is a copy: the clone may sit on somebody's WIP
        # branch, and that must not change what "current" means.
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        run_git(clone, "checkout", "-q", "-b", "wip")
        _write_skill(clone, "backend-dev", "backend-dev", body="half-written experiment")
        run_git(clone, "add", "-A")
        run_git(clone, "commit", "-q", "-m", "wip")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.is_clean

    def test_uncommitted_working_tree_changes_do_not_move_the_verdict(self, clone: Path, installed: Path) -> None:
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        _write_skill(clone, "qa", "qa", body="scratch edit, never committed")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[installed])
        assert drift.is_clean

    def test_first_search_dir_wins_like_the_loader(self, clone: Path, tmp_path: Path) -> None:
        # The comparison must judge the copy an agent would actually load.
        first, second = tmp_path / "first", tmp_path / "second"
        _write_skill(first, "backend-dev", "backend-dev", body="the old instructions")
        _write_skill(second, "backend-dev", "backend-dev")
        _write_skill(second, "qa", "qa")
        drift = measure_skill_drift(_clone_config(clone), search_dirs=[first, second])
        assert drift.stale == ("backend-dev",)

    def test_explicit_ref_is_honoured(self, clone: Path, installed: Path) -> None:
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        clone_config = SkillSourceClone(label="team/skills", paths=[str(clone)], ref="origin/main")
        drift = measure_skill_drift(clone_config, search_dirs=[installed])
        assert drift.ref == "origin/main"
        assert drift.is_clean

    def test_first_existing_clone_path_wins(self, clone: Path, installed: Path, tmp_path: Path) -> None:
        _write_skill(installed, "backend-dev", "backend-dev")
        _write_skill(installed, "qa", "qa")
        clone_config = SkillSourceClone(label="team/skills", paths=[str(tmp_path / "absent"), str(clone)])
        assert measure_skill_drift(clone_config, search_dirs=[installed]).is_clean


class TestUnmeasurable:
    def test_no_clone_anywhere_is_reported_not_swallowed(self, tmp_path: Path, installed: Path) -> None:
        # "I could not check" must never render as "it matches".
        clone_config = SkillSourceClone(label="team/skills", paths=[str(tmp_path / "nowhere")])
        drift = measure_skill_drift(clone_config, search_dirs=[installed])
        assert not drift.is_clean
        assert "no clone" in drift.unmeasurable
        assert drift.stale == ()

    def test_no_declared_path_is_reported(self, installed: Path) -> None:
        drift = measure_skill_drift(SkillSourceClone(label="team/skills"), search_dirs=[installed])
        assert "none declared" in drift.unmeasurable

    def test_unresolvable_ref_is_reported(self, clone: Path, installed: Path) -> None:
        clone_config = SkillSourceClone(label="team/skills", paths=[str(clone)], ref="origin/does-not-exist")
        # The declared ref does not resolve, but origin/HEAD does — the fallback
        # keeps a typo'd ref from blanking the whole comparison.
        assert measure_skill_drift(clone_config, search_dirs=[installed]).ref != ""

    def test_repo_without_skills_is_reported_rather_than_clean(self, tmp_path: Path, installed: Path) -> None:
        empty = make_git_repo(tmp_path / "empty-origin")
        run_git(tmp_path, "clone", "-q", str(empty), str(tmp_path / "empty-clone"))
        clone_config = SkillSourceClone(label="team/skills", paths=[str(tmp_path / "empty-clone")])
        drift = measure_skill_drift(clone_config, search_dirs=[installed])
        assert "lists no SKILL.md" in drift.unmeasurable
