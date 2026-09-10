"""Whole-repo bundle provisioning from the declared source, against real git (#4677)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.skill_bundle import (
    BundleInstallOutcome,
    MandatedBundleInstaller,
    parse_bundle_source,
    published_bundle_skills,
)

_GIT = shutil.which("git") or "/usr/bin/git"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([_GIT, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A real local git repo laid out like the declared bundle source."""
    repo = tmp_path / "remotes" / "obra" / "superpowers"
    for name in ("test-driven-development", "writing-plans", "using-superpowers"):
        (repo / "skills" / name).mkdir(parents=True)
        (repo / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    _git(repo.parent, "init", "--quiet", "-b", "main", "superpowers")
    _git(repo, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "superpowers")
    return repo


def _dep(source: str = "obra/superpowers") -> DeclaredDependency:
    return DeclaredDependency(
        kind="bundle", name="obra/superpowers", declared_in="apm.yml", remediation="run `t3 setup`", source=source
    )


def _installer(tmp_path: Path, remote: Path) -> MandatedBundleInstaller:
    return MandatedBundleInstaller(
        tmp_path / "cache",
        remote_base=f"{remote.parent.parent}/",
        excluded=("using-superpowers",),
    )


class TestParseBundleSource:
    def test_a_two_segment_spec_names_the_repo_and_its_pin(self) -> None:
        source = parse_bundle_source("obra/superpowers#1f20bef")

        assert source is not None
        assert (source.owner_repo, source.subpath, source.ref) == ("obra/superpowers", "", "1f20bef")

    def test_a_single_skill_spec_is_not_a_bundle(self) -> None:
        assert parse_bundle_source("souliane/skills/ac-python#d0008a3") is None


class TestPublishedBundleSkills:
    def test_skills_are_read_from_the_conventional_skills_directory(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        (checkout / "skills" / "writing-plans").mkdir(parents=True)
        (checkout / "skills" / "writing-plans" / "SKILL.md").write_text("---\n", encoding="utf-8")

        assert set(published_bundle_skills(checkout)) == {"writing-plans"}

    def test_a_repo_publishing_at_its_root_is_read_too(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        (checkout / "some-skill").mkdir(parents=True)
        (checkout / "some-skill" / "SKILL.md").write_text("---\n", encoding="utf-8")

        assert set(published_bundle_skills(checkout)) == {"some-skill"}


class TestMandatedBundleInstaller:
    def test_every_published_skill_becomes_loadable(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()

        report = _installer(tmp_path, remote).ensure(_dep(), link_dir=link_dir)

        assert report.outcome is BundleInstallOutcome.INSTALLED
        assert (link_dir / "test-driven-development" / "SKILL.md").is_file()
        assert (link_dir / "writing-plans" / "SKILL.md").is_file()

    def test_a_teatree_incompatible_skill_is_excluded_rather_than_installed(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()

        report = _installer(tmp_path, remote).ensure(_dep(), link_dir=link_dir)

        assert not (link_dir / "using-superpowers").exists()
        assert report.excluded == ("using-superpowers",)

    def test_running_twice_is_idempotent_with_the_same_end_state(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        installer = _installer(tmp_path, remote)

        installer.ensure(_dep(), link_dir=link_dir)
        second = installer.ensure(_dep(), link_dir=link_dir)

        assert second.outcome is BundleInstallOutcome.ALREADY_PRESENT
        assert (link_dir / "writing-plans" / "SKILL.md").is_file()

    def test_a_skill_already_loadable_from_elsewhere_is_never_displaced(self, tmp_path: Path, remote: Path) -> None:
        link_dir = tmp_path / "skills"
        (link_dir / "writing-plans").mkdir(parents=True)
        (link_dir / "writing-plans" / "SKILL.md").write_text("local override\n", encoding="utf-8")

        _installer(tmp_path, remote).ensure(_dep(), link_dir=link_dir)

        assert (link_dir / "writing-plans" / "SKILL.md").read_text(encoding="utf-8") == "local override\n"

    def test_an_unreachable_source_is_reported_rather_than_passed_off_as_installed(self, tmp_path: Path) -> None:
        link_dir = tmp_path / "skills"
        link_dir.mkdir()
        installer = MandatedBundleInstaller(tmp_path / "cache", remote_base=f"{tmp_path / 'nowhere'}/")

        report = installer.ensure(_dep(), link_dir=link_dir)

        assert report.outcome is BundleInstallOutcome.UNAVAILABLE
        assert report.unavailable

    def test_the_declared_pin_is_what_gets_checked_out(self, tmp_path: Path, remote: Path) -> None:
        head = _git(remote, "rev-parse", "HEAD")
        link_dir = tmp_path / "skills"
        link_dir.mkdir()

        report = _installer(tmp_path, remote).ensure(_dep(f"obra/superpowers#{head}"), link_dir=link_dir)

        assert report.outcome is BundleInstallOutcome.INSTALLED
        assert (link_dir / "writing-plans").resolve().parents[1].name.endswith(f"@{head}")
