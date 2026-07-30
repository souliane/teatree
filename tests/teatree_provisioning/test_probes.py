"""Provisioned-ness probes per dependency kind (#3652)."""

import json
from pathlib import Path

from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.probes import (
    binary_is_provisioned,
    integration_is_provisioned,
    skill_is_provisioned,
    unprovisioned,
)


def _skill(name: str) -> DeclaredDependency:
    return DeclaredDependency(kind="skill", name=name, declared_in="apm.yml", remediation="run `t3 setup`")


def _install_skill(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")


def _write_plugin_registry(home: Path, plugin_id: str, install_path: Path) -> None:
    plugins = home / ".claude" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"plugins": {plugin_id: [{"installPath": str(install_path)}]}}), encoding="utf-8"
    )


class TestSkillProvisioning:
    def test_a_skill_present_only_as_an_eval_fixture_is_unprovisioned(self, tmp_path: Path) -> None:
        """The exact shape of the live gap: ac-python exists under the eval corpus only."""
        fixtures = tmp_path / "evals" / "fixtures" / "skill_catalog" / "skills"
        _install_skill(fixtures, "ac-python")
        search_dirs = [tmp_path / "skills", tmp_path / "home" / ".claude" / "skills"]

        gaps = unprovisioned([_skill("ac-python")], search_dirs=search_dirs, home=tmp_path / "home")

        assert [gap.name for gap in gaps] == ["ac-python"]

    def test_a_skill_installed_on_a_search_dir_produces_no_finding(self, tmp_path: Path) -> None:
        installed = tmp_path / "home" / ".claude" / "skills"
        _install_skill(installed, "ac-python")

        assert unprovisioned([_skill("ac-python")], search_dirs=[installed], home=tmp_path / "home") == []

    def test_a_directory_without_a_skill_file_does_not_count_as_installed(self, tmp_path: Path) -> None:
        installed = tmp_path / "skills"
        (installed / "ac-python").mkdir(parents=True)

        assert unprovisioned([_skill("ac-python")], search_dirs=[installed], home=tmp_path) != []

    def test_the_eval_fixture_corpus_is_not_a_skill_search_dir(self, tmp_path: Path) -> None:
        """`skill_is_provisioned` answers only for dirs a loader actually reads."""
        fixtures = tmp_path / "evals" / "fixtures" / "skill_catalog" / "skills"
        _install_skill(fixtures, "ac-python")

        assert not skill_is_provisioned("ac-python", [tmp_path / "skills"])
        assert skill_is_provisioned("ac-python", [fixtures])


class TestBinaryProvisioning:
    def test_an_absent_binary_is_unprovisioned(self, tmp_path: Path) -> None:
        dep = DeclaredDependency(kind="binary", name="jq", declared_in="pyproject.toml", remediation="install jq")

        gaps = unprovisioned([dep], search_dirs=[], home=tmp_path, which=lambda _name: None)

        assert [gap.name for gap in gaps] == ["jq"]

    def test_a_binary_on_path_produces_no_finding(self, tmp_path: Path) -> None:
        dep = DeclaredDependency(kind="binary", name="jq", declared_in="pyproject.toml", remediation="install jq")

        assert unprovisioned([dep], search_dirs=[], home=tmp_path, which=lambda _name: "/usr/bin/jq") == []

    def test_the_probe_keys_on_path_resolution_alone(self) -> None:
        assert binary_is_provisioned("jq", lambda _name: "/usr/bin/jq")
        assert not binary_is_provisioned("jq", lambda _name: None)


class TestIntegrationProvisioning:
    def test_an_enabled_plugin_with_no_resolvable_install_path_is_unprovisioned(self, tmp_path: Path) -> None:
        dep = DeclaredDependency(
            kind="integration", name="t3@souliane", declared_in="settings.json", remediation="run `t3 setup`"
        )
        _write_plugin_registry(tmp_path, "t3@souliane", tmp_path / "gone")

        assert [gap.name for gap in unprovisioned([dep], search_dirs=[], home=tmp_path)] == ["t3@souliane"]

    def test_an_enabled_plugin_installed_at_a_real_path_produces_no_finding(self, tmp_path: Path) -> None:
        dep = DeclaredDependency(
            kind="integration", name="t3@souliane", declared_in="settings.json", remediation="run `t3 setup`"
        )
        install_path = tmp_path / "plugin"
        install_path.mkdir()
        _write_plugin_registry(tmp_path, "t3@souliane", install_path)

        assert unprovisioned([dep], search_dirs=[], home=tmp_path) == []

    def test_an_unreadable_plugin_registry_reads_as_unprovisioned(self, tmp_path: Path) -> None:
        """`integration_is_provisioned` fails closed: an unreadable registry proves nothing."""
        assert not integration_is_provisioned("t3@souliane", tmp_path)
