"""``DoctorService.repair_symlinks`` — skill symlink reconciliation.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

from teatree.cli.doctor import DoctorService

from ._shared import _stage_home


class TestRepairSymlinks:
    def test_creates_missing_link(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "code").mkdir()
        (skills_dir / "code" / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (1, 0)
        assert (claude_skills / "code").is_symlink()

    def test_handles_empty_skills_dir(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "not-a-skill").mkdir()  # No SKILL.md.
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (0, 0)

    def test_fixes_wrong_target(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "code"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        wrong_target = tmp_path / "wrong"
        wrong_target.mkdir()
        (claude_skills / "code").symlink_to(wrong_target)

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (1, 1)

    def test_skips_real_directory(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "code"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        (claude_skills / "code").mkdir()

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (0, 0)

    def test_leaves_correct_link_unchanged(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "code"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()
        (claude_skills / "code").symlink_to(skill)

        created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

        assert (created, fixed) == (0, 0)
