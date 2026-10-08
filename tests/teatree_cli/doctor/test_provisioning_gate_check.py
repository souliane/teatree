"""`t3 doctor` FAILs on any declared-but-unprovisioned dependency (#3652, epic #3445)."""

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.doctor.checks_provisioning import _check_declared_dependencies_provisioned
from teatree.utils import git_run

_PIN = "d0008a3c1e5f4b2a9d8e7f6a5b4c3d2e1f0a9b8c"
_DECLARED_SKILLS = (f"souliane/skills/ac-python#{_PIN}", f"souliane/skills/ac-django#{_PIN}")
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

        assert f"souliane/skills/ac-python#{_PIN}" in output
        assert f"souliane/skills/ac-django#{_PIN}" in output
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
            _manifest_body(*_DECLARED_SKILLS, f"souliane/skills/ac-rust#{_PIN}"), encoding="utf-8"
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

    def test_an_absent_manifest_stays_a_warn(self, project_root: Path, home: Path) -> None:
        (project_root / "apm.yml").unlink()

        ok, output = _run(project_root, home)

        assert ok
        assert "WARN  Provisioning gate: apm.yml is not readable" in output
        assert "FAIL" not in output


class TestAManifestThatExistsButCannotBeReadFails:
    @pytest.mark.parametrize(
        "body",
        ["dependencies: [unclosed", "- just\n- a list\n", "dependencies:\n  pip: []\n"],
        ids=["unparsable", "not-a-mapping", "no-apm-list"],
    )
    def test_it_fails_naming_the_file_and_its_restore_and_is_not_merely_a_warn(
        self, project_root: Path, home: Path, body: str
    ) -> None:
        (project_root / "apm.yml").write_text(body, encoding="utf-8")

        ok, output = _run(project_root, home)

        assert not ok
        refusal = output[output.index("FAIL  ") :]
        assert str(project_root / "apm.yml") in refusal
        assert f"git -C {project_root} checkout HEAD -- apm.yml" in refusal
        assert "WARN  Provisioning gate" not in output


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
        assert f"souliane/skills/ac-python#{_PIN}" in output
        assert str(shadow / "SKILL.md") in output

    def test_an_install_symlink_into_another_repo_fails_naming_that_repo(
        self, project_root: Path, home: Path, tmp_path: Path
    ) -> None:
        foreign = _checkout(tmp_path / "teatree-clone", "https://github.com/souliane/teatree.git", "ac-python")
        (home / ".claude" / "skills" / "ac-python").symlink_to(foreign, target_is_directory=True)
        _install(home / ".claude" / "skills", "ac-django")

        ok, output = _run(project_root, home)

        assert not ok
        assert str(foreign.resolve()) in output
        assert f"`souliane/skills/ac-python#{_PIN}`" in output


class TestADeclaredSkillSpecMustPinAFullCommit:
    """#4769: a spec without a 40-hex ``#ref`` installs whatever its source holds on the day ``t3 setup`` runs."""

    @pytest.mark.parametrize(
        "spec",
        ["souliane/skills/ac-python", "souliane/skills/ac-python#main", "souliane/skills/ac-python#d0008a3"],
    )
    def test_an_installed_skill_declared_without_a_full_sha_fails_naming_the_spec(
        self, project_root: Path, home: Path, spec: str
    ) -> None:
        (project_root / "apm.yml").write_text(_manifest_body(spec, _DECLARED_SKILLS[1]), encoding="utf-8")
        for name in ("ac-python", "ac-django"):
            _install(home / ".claude" / "skills", name)

        ok, output = _run(project_root, home)

        assert not ok
        assert f"`{spec}` names no 40-hex commit" in output
        assert "ac-django" not in output

    def test_the_shipped_manifest_pins_every_skill_spec_to_a_full_sha(self, tmp_path: Path, home: Path) -> None:
        empty_skills = tmp_path / "no-skills"
        empty_skills.mkdir()

        _, output = _run(_TEATREE_ROOT, home, search_dirs=[empty_skills])

        assert "Declared dependency not provisioned: skill" in output, "the shipped manifest was not enumerated"
        assert "names no 40-hex commit" not in output


@pytest.fixture(autouse=True)
def _no_xdg_state_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def _standard_install(home: Path, name: str) -> Path:
    real = _install(home / ".agents" / "skills", name)
    (home / ".claude" / "skills" / name).symlink_to(Path("../../.agents/skills") / name)
    return real


def _both_installed(home: Path, *, except_name: str = "") -> None:
    for name in ("ac-python", "ac-django"):
        if name != except_name:
            _standard_install(home, name)


def _write_record(home: Path, **skills: dict[str, str]) -> None:
    lock = home / ".agents" / ".skill-lock.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"version": 3, "skills": skills}), encoding="utf-8")


def _matching_record(home: Path) -> None:
    _write_record(home, **{name: {"source": "souliane/skills", "ref": _PIN} for name in ("ac-python", "ac-django")})


class TestAnInstallMustBeAnInstallerManagedCopy:
    """A pinned skill that resolves into a checkout or any tree no installer manages is a FAIL (#4769)."""

    def _assert_refused(self, project_root: Path, home: Path, offender: Path) -> None:
        ok, output = _run(project_root, home)

        assert not ok
        assert f"FAIL  Declared skill 'ac-python' at {offender}" in output
        assert "remove the link" in output
        assert f"`souliane/skills/ac-python#{_PIN}`" in output
        assert "t3 setup" in output
        assert "ac-django" not in output

    def test_the_standard_layout_a_real_directory_and_a_home_that_is_a_git_repo_are_silent(
        self, project_root: Path, home: Path
    ) -> None:
        (home / ".git").mkdir()
        _standard_install(home, "ac-python")
        _install(home / ".claude" / "skills", "ac-django")
        _matching_record(home)

        ok, output = _run(project_root, home)

        assert ok
        assert "FAIL" not in output

    def test_a_link_into_a_clone_of_the_declared_repo_fails(
        self, project_root: Path, home: Path, tmp_path: Path
    ) -> None:
        clone = _checkout(tmp_path / "skills-clone", "git@github.com:souliane/skills.git", "ac-python")
        (home / ".claude" / "skills" / "ac-python").symlink_to(clone, target_is_directory=True)
        _standard_install(home, "ac-django")

        self._assert_refused(project_root, home, home / ".claude" / "skills" / "ac-python")

    def test_a_link_into_the_overlay_tree_fails_although_it_is_no_checkout(
        self, project_root: Path, home: Path, tmp_path: Path
    ) -> None:
        overlay_copy = _install(tmp_path / "overlay" / "skills", "ac-python")
        (home / ".claude" / "skills" / "ac-python").symlink_to(overlay_copy, target_is_directory=True)
        _standard_install(home, "ac-django")

        self._assert_refused(project_root, home, home / ".claude" / "skills" / "ac-python")

    def test_a_replaced_front_door_fails_although_agents_holds_an_intact_copy(
        self, project_root: Path, home: Path, tmp_path: Path
    ) -> None:
        clone = _checkout(tmp_path / "skills-clone", "git@github.com:souliane/skills.git", "ac-python")
        _install(home / ".agents" / "skills", "ac-python")
        (home / ".claude" / "skills" / "ac-python").symlink_to(clone, target_is_directory=True)
        _standard_install(home, "ac-django")
        order = [project_root / "skills", home / ".agents" / "skills", home / ".claude" / "skills"]

        ok, output = _run(project_root, home, search_dirs=order)

        assert not ok
        assert f"FAIL  Declared skill 'ac-python' at {home / '.claude' / 'skills' / 'ac-python'}" in output

    def test_a_references_dir_linked_into_a_clone_fails(self, project_root: Path, home: Path, tmp_path: Path) -> None:
        skill = _standard_install(home, "ac-python")
        _standard_install(home, "ac-django")
        outside = tmp_path / "skills-clone" / "references"
        outside.mkdir(parents=True)
        (skill / "references").symlink_to(outside, target_is_directory=True)

        self._assert_refused(project_root, home, home / ".agents" / "skills" / "ac-python")

    def test_an_install_root_that_is_itself_a_link_into_a_clone_fails(
        self, project_root: Path, home: Path, tmp_path: Path
    ) -> None:
        clone_skills = _checkout(tmp_path / "skills-clone", "git@github.com:souliane/skills.git", "ac-python").parent
        _install(clone_skills, "ac-django")
        (home / ".claude" / "skills").rmdir()
        (home / ".claude" / "skills").symlink_to(clone_skills, target_is_directory=True)

        ok, output = _run(project_root, home)

        assert not ok
        assert f"FAIL  Declared skill 'ac-python' at {home / '.claude' / 'skills' / 'ac-python'}" in output
        assert "inside the git checkout" in output

    def test_a_real_directory_that_is_a_git_checkout_fails(self, project_root: Path, home: Path) -> None:
        skill = _install(home / ".claude" / "skills", "ac-python")
        git_run.run_strict(repo=str(skill), args=["init", "-q"])
        _install(home / ".claude" / "skills", "ac-django")

        self._assert_refused(project_root, home, home / ".claude" / "skills" / "ac-python")

    def test_a_declared_whole_repo_entry_without_a_commit_fails(self, project_root: Path, home: Path) -> None:
        (project_root / "apm.yml").write_text(_manifest_body(*_DECLARED_SKILLS, "obra/superpowers"), encoding="utf-8")
        _both_installed(home)
        _matching_record(home)

        ok, output = _run(project_root, home)

        assert not ok
        assert "`obra/superpowers` names no 40-hex commit" in output


class TestTheInstallRecordProvesTheRequestedRef:
    def test_a_matching_record_is_silent(self, project_root: Path, home: Path) -> None:
        _both_installed(home)
        _matching_record(home)

        ok, output = _run(project_root, home)

        assert ok
        assert "FAIL" not in output
        assert "UNVERIFIED" not in output

    @pytest.mark.parametrize(
        "entry",
        [
            {"source": "souliane/skills"},
            {"source": "souliane/skills", "ref": "a" * 40},
            {"source": "someone/else", "ref": _PIN},
        ],
        ids=["no-ref", "other-ref", "other-source"],
    )
    def test_a_record_naming_another_ref_or_source_fails_and_says_what_it_proves(
        self, project_root: Path, home: Path, entry: dict[str, str]
    ) -> None:
        _both_installed(home)
        _write_record(home, **{"ac-python": entry, "ac-django": {"source": "souliane/skills", "ref": _PIN}})

        ok, output = _run(project_root, home)

        assert not ok
        assert "FAIL  Declared skill 'ac-python'" in output
        assert "requested ref" in output
        assert "ac-django" not in output

    def test_a_record_file_without_an_entry_for_an_installed_skill_fails(self, project_root: Path, home: Path) -> None:
        _both_installed(home)
        _write_record(home, **{"ac-django": {"source": "souliane/skills", "ref": _PIN}})

        ok, output = _run(project_root, home)

        assert not ok
        assert "holds no entry for 'ac-python'" in output

    @pytest.mark.parametrize("body", [None, "{garbage", json.dumps({"version": 4, "skills": {}})])
    def test_an_absent_unparsable_or_other_schema_record_is_one_unverified_warn(
        self, project_root: Path, home: Path, body: str | None
    ) -> None:
        _both_installed(home)
        if body is not None:
            (home / ".agents" / ".skill-lock.json").write_text(body, encoding="utf-8")

        ok, output = _run(project_root, home)

        assert ok
        assert output.count("UNVERIFIED") == 1
        assert "FAIL" not in output

    def test_the_record_is_read_where_xdg_state_home_puts_it_never_from_a_stale_agents_copy(
        self, project_root: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _both_installed(home)
        _write_record(
            home, **{name: {"source": "souliane/skills", "ref": "a" * 40} for name in ("ac-python", "ac-django")}
        )
        state = tmp_path / "state" / "skills"
        state.mkdir(parents=True)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        (state / ".skill-lock.json").write_text(
            json.dumps(
                {
                    "version": 3,
                    "skills": {n: {"source": "souliane/skills", "ref": _PIN} for n in ("ac-python", "ac-django")},
                }
            ),
            encoding="utf-8",
        )

        ok, output = _run(project_root, home)

        assert ok
        assert "FAIL" not in output
