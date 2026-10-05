from pathlib import Path

import pytest

from teatree.provisioning.declared import skills_declared_in_apm_manifest

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestTheDeclaredSetSplitsPluginCarriedFromApmInstalled:
    """`architecture-design` is teatree's own; the `ac-*` companions are not."""

    def test_the_architecture_skill_is_plugin_carried_not_apm_installed(self) -> None:
        declared = {dep.name for dep in skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")}
        assert "architecture-design" not in declared
        assert (_REPO_ROOT / "skills" / "architecture-design" / "SKILL.md").is_file()

    def test_no_plugin_skill_shares_a_bare_name_with_a_declared_dependency(self) -> None:
        # A plugin copy under a declared name satisfies the provisioning probe on its own
        # and shadows the external skill at load time, so the two can never coexist.
        declared = skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")
        plugin_carried = [dep.name for dep in declared if (_REPO_ROOT / "skills" / dep.name).exists()]
        assert plugin_carried == []

    @pytest.mark.parametrize("name", ["ac-reviewing-codebase", "ac-python", "ac-django"])
    def test_each_companion_skill_is_mandated_from_its_own_repo(self, name: str) -> None:
        declared = {dep.name: dep for dep in skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")}
        assert name in declared, f"{name} is not mandated in apm.yml"
        assert declared[name].source.startswith("souliane/skills/"), declared[name].source
