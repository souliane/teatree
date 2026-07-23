"""Mandated-skill provisioning from the declared source, against real git (#3652)."""

import subprocess
from pathlib import Path

import pytest

from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.skill_source import InstallOutcome, MandatedSkillInstaller, parse_skill_source


def _git(repo: Path, *args: str) -> str:
    command = ["git", *args]
    return subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A real local git repo laid out like the declared skills source."""
    repo = tmp_path / "remotes" / "souliane" / "skills"
    (repo / "ac-python").mkdir(parents=True)
    (repo / "ac-python" / "SKILL.md").write_text("---\nname: ac-python\n---\n", encoding="utf-8")
    _git(repo.parent, "init", "--quiet", "-b", "main", "skills")
    _git(repo, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "skills")
    return repo


def _dep(source: str = "souliane/skills/ac-python") -> DeclaredDependency:
    return DeclaredDependency(
        kind="skill", name="ac-python", declared_in="apm.yml", remediation="run `t3 setup`", source=source
    )


class TestParseSkillSource:
    def test_owner_repo_subpath_and_ref_are_split(self) -> None:
        source = parse_skill_source("souliane/skills/ac-python#d0008a3")

        assert source is not None
        assert (source.owner_repo, source.subpath, source.ref) == ("souliane/skills", "ac-python", "d0008a3")

    def test_a_bundle_dependency_without_a_subpath_names_no_single_skill(self) -> None:
        assert parse_skill_source("obra/superpowers#1f20bef") is None


class TestMandatedSkillInstaller:
    def test_an_absent_skill_is_installed_and_becomes_loadable(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base=f"{remote.parent.parent}/")

        outcome = installer.ensure(_dep(), link_dir=link_dir)

        assert outcome is InstallOutcome.INSTALLED
        assert (link_dir / "ac-python" / "SKILL.md").is_file()

    def test_running_twice_is_idempotent_with_the_same_end_state(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base=f"{remote.parent.parent}/")
        installer.ensure(_dep(), link_dir=link_dir)
        before = (link_dir / "ac-python").resolve()

        outcome = installer.ensure(_dep(), link_dir=link_dir)

        assert outcome is InstallOutcome.ALREADY_PRESENT
        assert (link_dir / "ac-python").resolve() == before
        assert (link_dir / "ac-python" / "SKILL.md").is_file()

    def test_an_unreachable_source_reports_unavailable_rather_than_raising(self, tmp_path: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base=f"{tmp_path / 'nowhere'}/")

        assert installer.ensure(_dep(), link_dir=link_dir) is InstallOutcome.UNAVAILABLE

    def test_a_source_without_the_declared_subpath_reports_unavailable(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base=f"{remote.parent.parent}/")

        outcome = installer.ensure(_dep("souliane/skills/ac-absent"), link_dir=link_dir)

        assert outcome is InstallOutcome.UNAVAILABLE

    def test_a_pinned_ref_the_source_does_not_carry_reports_unavailable(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base=f"{remote.parent.parent}/")

        outcome = installer.ensure(_dep("souliane/skills/ac-python#deadbee"), link_dir=link_dir)

        assert outcome is InstallOutcome.UNAVAILABLE

    def test_a_stale_symlink_without_the_skill_is_replaced(self, tmp_path: Path, remote: Path) -> None:
        # A pre-existing link that does NOT resolve to a SKILL.md is re-pointed at
        # the real source, not left dangling.
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        empty_target = tmp_path / "empty"
        empty_target.mkdir()
        stale = link_dir / "ac-python"
        stale.symlink_to(empty_target)
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base=f"{remote.parent.parent}/")

        outcome = installer.ensure(_dep(), link_dir=link_dir)

        assert outcome is InstallOutcome.INSTALLED
        assert (link_dir / "ac-python" / "SKILL.md").is_file()
        assert (link_dir / "ac-python").resolve() != empty_target.resolve()

    def test_two_refs_of_one_repo_get_separate_checkouts(self, tmp_path: Path, remote: Path) -> None:
        head = _git(remote, "rev-parse", "HEAD")
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base=f"{remote.parent.parent}/")
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        other_dir = tmp_path / "other"
        other_dir.mkdir()

        installer.ensure(_dep("souliane/skills/ac-python"), link_dir=link_dir)
        installer.ensure(_dep(f"souliane/skills/ac-python#{head}"), link_dir=other_dir)

        assert len(list((tmp_path / "cache").iterdir())) == 2
