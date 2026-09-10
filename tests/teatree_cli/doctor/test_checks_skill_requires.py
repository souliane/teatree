"""A REQUIRED-tier `requires:` that resolves to nothing is reported, not warn-and-passed (#4677).

Warn-and-pass is right for a genuinely optional external skill and wrong for one whose
source the manifest mandates: three teatree skills delegate their methodology to
`obra/superpowers`, so an absent bundle made every coding and planning dispatch resolve
`test-driven-development` / `writing-plans` / `systematic-debugging` to nothing while
every surface still read healthy.
"""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.doctor.checks_skill_requires import _check_required_tier_requires_resolve

_BUNDLE = "obra/superpowers#1f20bef"


def _manifest(root: Path, *specs: str) -> None:
    entries = "".join(f"  - {spec}\n" for spec in specs)
    (root / "apm.yml").write_text(f"name: souliane/teatree\ndependencies:\n  apm:\n{entries}", encoding="utf-8")


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "teatree"
    skill = root / "skills" / "code"
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        "---\nname: code\nrequires:\n  - rules\n  - test-driven-development\n---\n", encoding="utf-8"
    )
    (root / "skills" / "rules").mkdir()
    (root / "skills" / "rules" / "SKILL.md").write_text("---\nname: rules\n---\n", encoding="utf-8")
    _manifest(root, _BUNDLE)
    return root


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    """A fetched bundle checkout publishing the delegated methodology skill."""
    published = tmp_path / "cache" / "obra-superpowers@1f20bef" / "skills" / "test-driven-development"
    published.mkdir(parents=True)
    (published / "SKILL.md").write_text("---\nname: test-driven-development\n---\n", encoding="utf-8")
    return tmp_path / "cache"


def _run(project_root: Path, cache_root: Path, search_dirs: list[Path]) -> tuple[bool, str]:
    holder: dict[str, bool] = {}
    app = typer.Typer()

    @app.command()
    def run() -> None:
        holder["ok"] = _check_required_tier_requires_resolve(
            project_root=project_root, cache_root=cache_root, search_dirs=search_dirs
        )

    result = CliRunner().invoke(app, [])
    return holder["ok"], result.output


class TestMandatedRequiresIsAFailure:
    def test_a_mandated_requires_that_resolves_to_nothing_fails(
        self, project_root: Path, cache_root: Path, tmp_path: Path
    ) -> None:
        ok, output = _run(project_root, cache_root, [tmp_path / "empty-skills"])

        assert not ok
        assert "FAIL" in output
        assert "test-driven-development" in output

    def test_the_failure_names_the_skill_that_delegates_to_it(
        self, project_root: Path, cache_root: Path, tmp_path: Path
    ) -> None:
        _, output = _run(project_root, cache_root, [tmp_path / "empty-skills"])

        assert "'code'" in output

    def test_the_failure_names_the_declaration_that_mandates_it(
        self, project_root: Path, cache_root: Path, tmp_path: Path
    ) -> None:
        _, output = _run(project_root, cache_root, [tmp_path / "empty-skills"])

        assert "obra/superpowers" in output
        assert "t3 setup" in output

    def test_an_installed_mandated_requires_is_silent(
        self, project_root: Path, cache_root: Path, tmp_path: Path
    ) -> None:
        installed = tmp_path / "skills" / "test-driven-development"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text("---\n", encoding="utf-8")

        ok, output = _run(project_root, cache_root, [tmp_path / "skills"])

        assert ok
        assert output.strip() == ""


class TestUnmandatedRequiresStaysAWarning:
    def test_an_optional_external_requires_warns_rather_than_failing(self, project_root: Path, tmp_path: Path) -> None:
        # Nothing fetched and nothing declared publishes it: genuinely optional.
        _manifest(project_root)

        ok, output = _run(project_root, tmp_path / "no-cache", [tmp_path / "empty-skills"])

        assert ok
        assert "WARN" in output
        assert "FAIL" not in output
        assert "test-driven-development" in output

    def test_a_requires_naming_an_in_repo_skill_is_never_reported(
        self, project_root: Path, cache_root: Path, tmp_path: Path
    ) -> None:
        _, output = _run(project_root, cache_root, [tmp_path / "empty-skills"])

        assert "'rules'" not in output


class TestASingleSkillDeclarationAlsoMandates:
    def test_a_declared_single_skill_that_is_absent_fails(self, project_root: Path, tmp_path: Path) -> None:
        _manifest(project_root, "souliane/skills/test-driven-development#abc1234")

        ok, output = _run(project_root, tmp_path / "no-cache", [tmp_path / "empty-skills"])

        assert not ok
        assert "test-driven-development" in output
