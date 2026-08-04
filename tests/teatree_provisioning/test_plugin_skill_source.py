"""A declared skill the plugin already ships is provisioned FROM the plugin (#3668).

The declaration mechanism and its fail-loud doctor gate already existed; the gap
was provisioning — every declared skill went out to a network clone, so on a box
with no reachable source the mandate stayed unmet and `t3 doctor check` FAILED on
skills whose content the plugin was already carrying.

The plugin's own ``skills/`` tree is the first source consulted, which makes a
plugin-carried skill — ``architecture-design`` — install offline and
deterministically. The remote clone is the path for a declared skill the plugin
does not carry: the ``ac-*`` companions, which are owned in ``souliane/skills``
and mandated from there rather than copied in-tree.
"""

from pathlib import Path

import pytest

from teatree.provisioning.declared import DeclaredDependency, skills_declared_in_apm_manifest
from teatree.provisioning.probes import skill_is_provisioned
from teatree.provisioning.skill_source import InstallOutcome, MandatedSkillInstaller

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _dependency(name: str, source: str = "someone/somewhere/some-skill#ref") -> DeclaredDependency:
    return DeclaredDependency(kind="skill", name=name, declared_in="apm.yml", remediation="", source=source)


@pytest.fixture
def plugin_skills(tmp_path: Path) -> Path:
    skills = tmp_path / "plugin-skills"
    (skills / "architecture-design").mkdir(parents=True)
    (skills / "architecture-design" / "SKILL.md").write_text("# architecture-design\n", encoding="utf-8")
    return skills


class TestPluginIsTheFirstSource:
    def test_a_plugin_carried_skill_installs_with_no_network(self, tmp_path: Path, plugin_skills: Path) -> None:
        link_dir = tmp_path / "runtime-skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", plugin_skills_dir=plugin_skills)

        outcome = installer.ensure(_dependency("architecture-design"), link_dir=link_dir)

        assert outcome is InstallOutcome.INSTALLED
        assert skill_is_provisioned("architecture-design", [link_dir])
        assert not (tmp_path / "cache").exists()

    def test_installing_twice_is_a_no_op(self, tmp_path: Path, plugin_skills: Path) -> None:
        link_dir = tmp_path / "runtime-skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", plugin_skills_dir=plugin_skills)
        installer.ensure(_dependency("architecture-design"), link_dir=link_dir)

        assert installer.ensure(_dependency("architecture-design"), link_dir=link_dir) is InstallOutcome.ALREADY_PRESENT

    def test_a_skill_the_plugin_does_not_carry_falls_back_to_the_declared_source(
        self, tmp_path: Path, plugin_skills: Path
    ) -> None:
        link_dir = tmp_path / "runtime-skills"
        link_dir.mkdir()
        # An unreachable remote: the point is that the plugin lookup did not swallow it.
        installer = MandatedSkillInstaller(
            tmp_path / "cache", plugin_skills_dir=plugin_skills, remote_base="file:///nonexistent/"
        )

        assert installer.ensure(_dependency("not-in-the-plugin"), link_dir=link_dir) is InstallOutcome.UNAVAILABLE

    def test_no_plugin_dir_configured_still_uses_the_declared_source(self, tmp_path: Path) -> None:
        link_dir = tmp_path / "runtime-skills"
        link_dir.mkdir()
        installer = MandatedSkillInstaller(tmp_path / "cache", remote_base="file:///nonexistent/")

        assert installer.ensure(_dependency("architecture-design"), link_dir=link_dir) is InstallOutcome.UNAVAILABLE


class TestTheDeclaredSetSplitsPluginCarriedFromApmInstalled:
    """`architecture-design` is teatree's own; the `ac-*` companions are not."""

    def test_the_architecture_skill_is_declared(self) -> None:
        declared = {dep.name for dep in skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")}
        assert "architecture-design" in declared

    def test_the_only_plugin_carried_declared_skill_is_the_architecture_skill(self) -> None:
        declared = skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")
        plugin_carried = [dep for dep in declared if (_REPO_ROOT / "skills" / dep.name / "SKILL.md").is_file()]
        assert {dep.name for dep in plugin_carried} == {"architecture-design"}

    @pytest.mark.parametrize("name", ["ac-reviewing-codebase", "ac-python", "ac-django"])
    def test_each_companion_skill_is_mandated_from_its_own_repo(self, name: str) -> None:
        # The plugin-first install path means a copy under `skills/` would satisfy
        # the mandate locally and hide a broken remote. These three are owned
        # elsewhere, so the declaration must be the only way they arrive.
        declared = {dep.name: dep for dep in skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")}
        assert name in declared, f"{name} is not mandated in apm.yml"
        assert declared[name].source.startswith("souliane/skills/"), declared[name].source
        assert not (_REPO_ROOT / "skills" / name).exists(), f"{name} is vendored in-tree and would mask the mandate"
