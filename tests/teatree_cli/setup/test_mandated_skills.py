"""`t3 setup` provisions the configuration-mandated skills idempotently (#3652)."""

import subprocess
from pathlib import Path

import pytest

from teatree.cli.setup.mandated_skills import MandatedSkillProvisioner
from teatree.provisioning.skill_source import MandatedSkillInstaller

_MANIFEST = (
    "name: souliane/teatree\ndependencies:\n    apm:\n    - obra/superpowers#1f20bef\n    - souliane/skills/ac-python\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S607 — git on PATH


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A teatree checkout declaring one mandated skill, sourced from a real local repo."""
    source = tmp_path / "remotes" / "souliane" / "skills"
    (source / "ac-python").mkdir(parents=True)
    (source / "ac-python" / "SKILL.md").write_text("---\nname: ac-python\n---\n", encoding="utf-8")
    _git(source.parent, "init", "--quiet", "-b", "main", "skills")
    _git(source, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(source, "config", "user.name", "t")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "skills")

    checkout = tmp_path / "teatree"
    checkout.mkdir()
    (checkout / "apm.yml").write_text(_MANIFEST, encoding="utf-8")
    return checkout


@pytest.fixture
def provisioner(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> MandatedSkillProvisioner:
    monkeypatch.setattr(
        "teatree.cli.setup.mandated_skills.MandatedSkillInstaller",
        lambda cache_root: _local_installer(cache_root, tmp_path),
    )
    return MandatedSkillProvisioner(repo, tmp_path / "home" / ".claude" / "skills", tmp_path / "cache")


def _local_installer(cache_root: Path, tmp_path: Path) -> MandatedSkillInstaller:
    return MandatedSkillInstaller(cache_root, remote_base=f"{tmp_path / 'remotes'}/")


class TestMandatedSkillProvisioner:
    def test_a_declared_but_absent_skill_becomes_loadable(self, provisioner: MandatedSkillProvisioner) -> None:
        lines: list[str] = []

        assert provisioner.provision(lines.append)
        assert (provisioner.skills_dir / "ac-python" / "SKILL.md").is_file()

    def test_re_running_is_idempotent_and_reaches_the_same_end_state(
        self, provisioner: MandatedSkillProvisioner
    ) -> None:
        provisioner.provision(lambda _line: None)
        target = (provisioner.skills_dir / "ac-python").resolve()
        lines: list[str] = []

        assert provisioner.provision(lines.append)
        assert (provisioner.skills_dir / "ac-python").resolve() == target
        assert any("already loadable" in line for line in lines)

    def test_an_unreachable_source_warns_instead_of_raising(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "teatree.cli.setup.mandated_skills.MandatedSkillInstaller",
            lambda cache_root: _local_installer(cache_root, tmp_path / "nowhere"),
        )
        lines: list[str] = []

        assert not MandatedSkillProvisioner(repo, tmp_path / "skills", tmp_path / "cache").provision(lines.append)
        assert any("WARN" in line and "ac-python" in line for line in lines)

    def test_a_checkout_with_no_manifest_warns_rather_than_reporting_success_silently(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        lines: list[str] = []

        assert MandatedSkillProvisioner(empty, tmp_path / "skills", tmp_path / "cache").provision(lines.append)
        assert any("WARN" in line for line in lines)
