"""`t3 setup` provisions the configuration-mandated skills and bundles idempotently (#3652, #4677)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.cli.setup.mandated_skills import MandatedSkillProvisioner
from teatree.provisioning.skill_bundle import MandatedBundleInstaller
from teatree.provisioning.skill_source import MandatedSkillInstaller


def _manifest(bundle_ref: str) -> str:
    return (
        "name: souliane/teatree\ndependencies:\n    apm:\n"
        f"    - obra/superpowers#{bundle_ref}\n    - souliane/skills/ac-python\n"
    )


_GIT = shutil.which("git") or "/usr/bin/git"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([_GIT, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


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

    bundle = tmp_path / "remotes" / "obra" / "superpowers"
    for name in ("writing-plans", "using-superpowers"):
        (bundle / "skills" / name).mkdir(parents=True)
        (bundle / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    _git(bundle.parent, "init", "--quiet", "-b", "main", "superpowers")
    _git(bundle, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(bundle, "config", "user.name", "t")
    _git(bundle, "add", "-A")
    _git(bundle, "commit", "--quiet", "-m", "superpowers")

    checkout = tmp_path / "teatree"
    checkout.mkdir()
    (checkout / "apm.yml").write_text(_manifest(_git(bundle, "rev-parse", "HEAD")), encoding="utf-8")
    return checkout


@pytest.fixture
def provisioner(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> MandatedSkillProvisioner:
    _redirect_installers(monkeypatch, tmp_path)
    return MandatedSkillProvisioner(repo, tmp_path / "home" / ".claude" / "skills", tmp_path / "cache")


def _redirect_installers(monkeypatch: pytest.MonkeyPatch, remotes_under: Path) -> None:
    monkeypatch.setattr(
        "teatree.cli.setup.mandated_skills.MandatedSkillInstaller",
        lambda cache_root, **kwargs: _local_installer(cache_root, remotes_under, **kwargs),
    )
    monkeypatch.setattr(
        "teatree.cli.setup.mandated_skills.MandatedBundleInstaller",
        lambda cache_root, **kwargs: MandatedBundleInstaller(
            cache_root, remote_base=f"{remotes_under / 'remotes'}/", **kwargs
        ),
    )


def _local_installer(cache_root: Path, tmp_path: Path, **kwargs: Path | None) -> MandatedSkillInstaller:
    """The real installer with only its remote redirected at a local bare repo.

    Every keyword the production call site passes is FORWARDED, never dropped, so the
    double keeps exercising the real two-source contract — the plugin's own tree first,
    the declared remote as the fallback — rather than a version of it that ignores
    whichever parameter the double was written before.
    """
    return MandatedSkillInstaller(cache_root, remote_base=f"{tmp_path / 'remotes'}/", **kwargs)


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
        _redirect_installers(monkeypatch, tmp_path / "nowhere")
        lines: list[str] = []

        assert not MandatedSkillProvisioner(repo, tmp_path / "skills", tmp_path / "cache").provision(lines.append)
        assert any("WARN" in line and "ac-python" in line for line in lines)

    def test_a_checkout_with_no_manifest_warns_rather_than_reporting_success_silently(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        lines: list[str] = []

        assert MandatedSkillProvisioner(empty, tmp_path / "skills", tmp_path / "cache").provision(lines.append)
        assert any("WARN" in line for line in lines)


class TestMandatedBundleProvisioner:
    """A REQUIRED whole-repo bundle is installed and REPORTED, never skipped (#4677)."""

    def test_every_skill_the_bundle_publishes_becomes_loadable(self, provisioner: MandatedSkillProvisioner) -> None:
        assert provisioner.provision(lambda _line: None)
        assert (provisioner.skills_dir / "writing-plans" / "SKILL.md").is_file()

    def test_a_teatree_incompatible_skill_is_never_installed(self, provisioner: MandatedSkillProvisioner) -> None:
        provisioner.provision(lambda _line: None)

        assert not (provisioner.skills_dir / "using-superpowers").exists()

    def test_the_bundle_is_named_in_the_output_rather_than_silently_skipped(
        self, provisioner: MandatedSkillProvisioner
    ) -> None:
        lines: list[str] = []

        provisioner.provision(lines.append)

        assert any("obra/superpowers" in line for line in lines)

    def test_re_running_reports_already_loadable_and_installs_nothing_new(
        self, provisioner: MandatedSkillProvisioner
    ) -> None:
        provisioner.provision(lambda _line: None)
        target = (provisioner.skills_dir / "writing-plans").resolve()
        lines: list[str] = []

        assert provisioner.provision(lines.append)
        assert (provisioner.skills_dir / "writing-plans").resolve() == target
        assert any("obra/superpowers" in line and "already loadable" in line for line in lines)

    def test_an_unreachable_bundle_warns_and_reports_failure(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _redirect_installers(monkeypatch, tmp_path / "nowhere")
        lines: list[str] = []

        assert not MandatedSkillProvisioner(repo, tmp_path / "skills", tmp_path / "cache").provision(lines.append)
        assert any("WARN" in line and "obra/superpowers" in line for line in lines)
