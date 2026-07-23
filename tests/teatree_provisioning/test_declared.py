"""The declared-dependency enumeration reads real configuration surfaces (#3652)."""

import json
from pathlib import Path

import pytest

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    binaries_declared_in_pyproject,
    declared_dependencies,
    integrations_declared_in_claude_settings,
    skills_declared_in_apm_manifest,
)

_DEFAULT_SPECS = (
    "obra/superpowers#1f20bef",
    "souliane/skills/ac-python#d0008a3",
    "souliane/skills/ac-django#d0008a3",
    "souliane/teatree",
)


def _manifest_body(*specs: str) -> str:
    entries = "".join(f"    - {spec}\n" for spec in specs or _DEFAULT_SPECS)
    return f"name: souliane/teatree\ndependencies:\n    apm:\n{entries}"


def _write_manifest(tmp_path: Path, body: str | None = None) -> Path:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(_manifest_body() if body is None else body, encoding="utf-8")
    return manifest


class TestSkillsDeclaredInApmManifest:
    def test_named_skill_dependencies_are_enumerated(self, tmp_path: Path) -> None:
        declared = skills_declared_in_apm_manifest(_write_manifest(tmp_path))

        assert [dep.name for dep in declared] == ["ac-python", "ac-django"]
        assert all(dep.kind == "skill" for dep in declared)
        assert all("apm.yml" in dep.declared_in for dep in declared)

    def test_a_newly_declared_skill_is_enumerated_with_no_code_change(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path, _manifest_body(*_DEFAULT_SPECS, "souliane/skills/ac-rust#abc1234"))

        assert "ac-rust" in {dep.name for dep in skills_declared_in_apm_manifest(manifest)}

    def test_the_fetch_source_round_trips_repo_ref_and_subpath(self, tmp_path: Path) -> None:
        declared = skills_declared_in_apm_manifest(_write_manifest(tmp_path))

        assert declared[0].source == "souliane/skills/ac-python#d0008a3"

    def test_remediation_names_the_command_that_provisions_it(self, tmp_path: Path) -> None:
        declared = skills_declared_in_apm_manifest(_write_manifest(tmp_path))

        assert "t3 setup" in declared[0].remediation

    def test_an_absent_manifest_raises_rather_than_reporting_zero_dependencies(self, tmp_path: Path) -> None:
        with pytest.raises(DeclarationUnreadableError):
            skills_declared_in_apm_manifest(tmp_path / "nope.yml")

    def test_unparsable_manifest_raises_rather_than_reporting_zero_dependencies(self, tmp_path: Path) -> None:
        with pytest.raises(DeclarationUnreadableError):
            skills_declared_in_apm_manifest(_write_manifest(tmp_path, "dependencies: [oh: no\n"))


class TestBinariesDeclaredInPyproject:
    def test_required_binaries_are_read_from_the_declared_table(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[tool.teatree.provisioning]\nrequired_binaries = ["direnv", "jq"]\n',
            encoding="utf-8",
        )

        declared = binaries_declared_in_pyproject(pyproject)

        assert [dep.name for dep in declared] == ["direnv", "jq"]
        assert all(dep.kind == "binary" for dep in declared)

    def test_a_missing_table_raises_rather_than_reporting_zero_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'x'\n", encoding="utf-8")

        with pytest.raises(DeclarationUnreadableError):
            binaries_declared_in_pyproject(pyproject)


class TestIntegrationsDeclaredInClaudeSettings:
    def test_enabled_plugins_are_enumerated_and_disabled_ones_are_not(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"enabledPlugins": {"t3@souliane": True, "off@vendorless": False}}),
            encoding="utf-8",
        )

        declared = integrations_declared_in_claude_settings(settings)

        assert [dep.name for dep in declared] == ["t3@souliane"]
        assert declared[0].kind == "integration"

    def test_absent_settings_declares_nothing_rather_than_raising(self, tmp_path: Path) -> None:
        assert integrations_declared_in_claude_settings(tmp_path / "settings.json") == []


class TestDeclaredDependencies:
    @pytest.fixture
    def home(self, tmp_path: Path) -> Path:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"t3@souliane": True}}), encoding="utf-8"
        )
        return home

    def test_every_configuration_surface_contributes_to_one_enumeration(self, tmp_path: Path, home: Path) -> None:
        _write_manifest(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.teatree.provisioning]\nrequired_binaries = ["jq"]\n', encoding="utf-8"
        )

        enumeration = declared_dependencies(project_root=tmp_path, home=home)

        assert {dep.kind for dep in enumeration.dependencies} == {"skill", "binary", "integration"}
        assert enumeration.unreadable == []

    def test_one_unreadable_surface_neither_hides_the_others_nor_passes_as_empty(
        self, tmp_path: Path, home: Path
    ) -> None:
        _write_manifest(tmp_path)

        enumeration = declared_dependencies(project_root=tmp_path, home=home)

        assert "ac-python" in {dep.name for dep in enumeration.dependencies}
        assert any("pyproject.toml" in reason for reason in enumeration.unreadable)
