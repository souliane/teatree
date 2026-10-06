"""`t3 doctor` FAILs on any declared-but-unprovisioned dependency (#3652, epic #3445)."""

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.doctor.checks_provisioning import _check_declared_dependencies_provisioned
from teatree.utils import git_run

_DECLARED_SKILLS = ("souliane/skills/ac-python#d0008a3", "souliane/skills/ac-django#d0008a3")
#: The real checkout, for the tests that gate the SHIPPED manifest rather than a fixture.
_TEATREE_ROOT = Path(__file__).resolve().parents[3]


def _manifest_body(*specs: str) -> str:
    entries = "".join(f"    - {spec}\n" for spec in specs)
    return f"name: souliane/teatree\ndependencies:\n    apm:\n{entries}"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "teatree"
    (root / "evals" / "fixtures" / "skill_catalog" / "skills" / "ac-python").mkdir(parents=True)
    (root / "evals" / "fixtures" / "skill_catalog" / "skills" / "ac-python" / "SKILL.md").write_text(
        "---\nname: ac-python\n---\n", encoding="utf-8"
    )
    (root / "apm.yml").write_text(_manifest_body(*_DECLARED_SKILLS), encoding="utf-8")
    (root / "pyproject.toml").write_text('[tool.teatree.provisioning]\nrequired_binaries = ["jq"]\n', encoding="utf-8")
    (root / "skills").mkdir()
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".claude" / "skills").mkdir(parents=True)
    (path / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {}}), encoding="utf-8")
    return path


def _run(
    project_root: Path,
    home: Path,
    *,
    which: object = None,
    search_dirs: list[Path] | None = None,
) -> tuple[bool, str]:
    app = typer.Typer()
    dirs = [project_root / "skills", home / ".claude" / "skills"] if search_dirs is None else search_dirs

    @app.command()
    def main() -> None:
        ok = _check_declared_dependencies_provisioned(
            project_root=project_root,
            home=home,
            search_dirs=dirs,
            which=which or (lambda _name: "/usr/bin/jq"),
        )
        raise typer.Exit(code=0 if ok else 1)

    result = CliRunner().invoke(app, [])
    return result.exit_code == 0, result.output


class TestMandatedSkillAbsent:
    def test_a_skill_present_only_as_an_eval_fixture_fails_and_is_named(self, project_root: Path, home: Path) -> None:
        ok, output = _run(project_root, home)

        assert not ok
        assert "FAIL" in output
        assert "ac-python" in output
        assert "ac-django" in output

    def test_the_failure_carries_the_exact_remediation(self, project_root: Path, home: Path) -> None:
        _, output = _run(project_root, home)

        assert "souliane/skills/ac-python#d0008a3" in output
        assert "souliane/skills/ac-django#d0008a3" in output
        assert "t3 setup" in output
        assert "apm install" not in output

    def test_the_failure_names_where_the_dependency_is_declared(self, project_root: Path, home: Path) -> None:
        _, output = _run(project_root, home)

        assert "apm.yml" in output


class TestProvisionedDependencyIsSilent:
    def test_an_installed_mandated_skill_produces_no_finding(self, project_root: Path, home: Path) -> None:
        for name in ("ac-python", "ac-django"):
            skill = home / ".claude" / "skills" / name
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

        ok, output = _run(project_root, home)

        assert ok
        assert "FAIL" not in output


class TestConfigDrivenEnumeration:
    def test_a_newly_declared_skill_is_checked_without_touching_the_check(self, project_root: Path, home: Path) -> None:
        for name in ("ac-python", "ac-django"):
            skill = home / ".claude" / "skills" / name
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        (project_root / "apm.yml").write_text(
            _manifest_body(*_DECLARED_SKILLS, "souliane/skills/ac-rust#d0008a3"), encoding="utf-8"
        )

        ok, output = _run(project_root, home)

        assert not ok
        assert "ac-rust" in output

    def test_a_newly_declared_binary_is_checked_without_touching_the_check(
        self, project_root: Path, home: Path
    ) -> None:
        (project_root / "pyproject.toml").write_text(
            '[tool.teatree.provisioning]\nrequired_binaries = ["jq", "shellcheck"]\n', encoding="utf-8"
        )

        _, output = _run(project_root, home, which=lambda name: None if name == "shellcheck" else "/usr/bin/jq")

        assert "shellcheck" in output


class TestTheShippedManifestMandatesTheCompanionSkills:
    """Against the REAL apm.yml: the three companions FAIL when absent.

    The gate only holds while each companion is mandated and NOT carried in-tree.
    """

    @pytest.mark.parametrize("name", ["ac-reviewing-codebase", "ac-python", "ac-django"])
    def test_an_absent_companion_skill_is_a_named_fail_with_its_install_line(
        self, tmp_path: Path, home: Path, name: str
    ) -> None:
        empty_skills = tmp_path / "no-skills"
        empty_skills.mkdir()

        _, output = _run(_TEATREE_ROOT, home, search_dirs=[empty_skills])

        assert f"FAIL  Declared dependency not provisioned: skill '{name}'" in output
        assert f"souliane/skills/{name}#" in output
        assert "t3 setup" in output

    def test_the_companion_skills_are_not_shipped_in_the_plugins_own_skills_tree(self) -> None:
        # A copy under `skills/` satisfies the mandate from the plugin-first
        # install path, so vendoring one silently turns the gate above green.
        for name in ("ac-reviewing-codebase", "ac-python", "ac-django"):
            assert not (_TEATREE_ROOT / "skills" / name).exists(), name


class TestSilenceIsNeverAnOutcome:
    def test_an_unreadable_declaration_surface_still_reports(self, tmp_path: Path, home: Path) -> None:
        empty_root = tmp_path / "no-manifest"
        empty_root.mkdir()

        ok, output = _run(empty_root, home)

        assert ok, "an unreadable manifest is a WARN, not a gate failure"
        assert "WARN" in output
        assert output.strip(), "the provisioning gate must never emit nothing"

    def test_one_unreadable_surface_does_not_suppress_another_surface_failure(
        self, project_root: Path, home: Path
    ) -> None:
        (project_root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

        ok, output = _run(project_root, home)

        assert not ok
        assert "ac-python" in output
        assert "WARN" in output


def _install(root: Path, name: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return skill


def _checkout(path: Path, origin: str, name: str) -> Path:
    path.mkdir()
    git_run.run_strict(repo=str(path), args=["init", "-q"])
    git_run.run_strict(repo=str(path), args=["remote", "add", "origin", origin])
    return _install(path / "skills", name)


class TestADeclaredSkillSatisfiedByAnotherSource:
    """#4769: a declared skill name resolving to something other than its declared source is a FAIL."""

    def test_a_local_skills_folder_shadowing_the_pin_fails_naming_both_sides(
        self, project_root: Path, home: Path
    ) -> None:
        shadow = _install(project_root / "skills", "ac-python")
        for name in ("ac-python", "ac-django"):
            _install(home / ".claude" / "skills", name)

        ok, output = _run(project_root, home)

        assert not ok
        assert "souliane/skills/ac-python#d0008a3" in output
        assert str(shadow / "SKILL.md") in output

    def test_an_install_symlink_into_another_repo_fails_naming_that_repo(
        self, project_root: Path, home: Path, tmp_path: Path
    ) -> None:
        foreign = _checkout(tmp_path / "teatree-clone", "https://github.com/souliane/teatree.git", "ac-python")
        (home / ".claude" / "skills" / "ac-python").symlink_to(foreign, target_is_directory=True)
        _install(home / ".claude" / "skills", "ac-django")

        ok, output = _run(project_root, home)

        assert not ok
        assert "a checkout of souliane/teatree, not the declared `souliane/skills/ac-python#d0008a3`" in output

    def test_an_install_symlink_into_a_live_clone_of_the_declared_repo_is_silent(
        self, project_root: Path, home: Path, tmp_path: Path
    ) -> None:
        clone = _checkout(tmp_path / "skills-clone", "git@github.com:souliane/skills.git", "ac-python")
        (home / ".claude" / "skills" / "ac-python").symlink_to(clone, target_is_directory=True)
        _install(home / ".claude" / "skills", "ac-django")

        ok, output = _run(project_root, home)

        assert ok
        assert "FAIL" not in output
