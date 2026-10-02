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

    def test_the_plugin_carries_the_two_skills_its_own_code_dispatches(self) -> None:
        # `ac-reviewing-codebase` is OWNED elsewhere but must ALSO ship in-tree:
        # it is the default skill of
        # `ArchitecturalReviewScanner`, which resolves against `default_search_dirs()`
        # — the plugin `skills/` dir plus two agent dirs that do not exist in CI or on
        # a fresh box. Without the in-tree copy the scanner's default resolves to
        # nothing and every periodic review runs with zero guidance, which is #3353.
        declared = skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")
        plugin_carried = [dep for dep in declared if (_REPO_ROOT / "skills" / dep.name / "SKILL.md").is_file()]
        assert {dep.name for dep in plugin_carried} == {"ac-reviewing-codebase"}

    @pytest.mark.parametrize("name", ["ac-reviewing-codebase", "ac-python", "ac-django"])
    def test_each_companion_skill_is_mandated_from_its_own_repo(self, name: str) -> None:
        declared = {dep.name: dep for dep in skills_declared_in_apm_manifest(_REPO_ROOT / "apm.yml")}
        assert name in declared, f"{name} is not mandated in apm.yml"
        assert declared[name].source.startswith("souliane/skills/"), declared[name].source

    @pytest.mark.parametrize("name", ["ac-python", "ac-django"])
    def test_a_companion_no_code_dispatches_is_not_also_vendored(self, name: str) -> None:
        # The plugin-first install path means a copy under `skills/` would satisfy the
        # mandate locally and hide a broken remote. These two are only ever loaded by
        # an agent reading them, so the declaration must be the only way they arrive.
        # `ac-reviewing-codebase` is deliberately excluded: teatree's own scanner names
        # it as a default and must be able to resolve it offline (#3353).
        assert not (_REPO_ROOT / "skills" / name).exists(), f"{name} is vendored in-tree and would mask the mandate"
