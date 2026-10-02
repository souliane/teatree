from pathlib import Path

from teatree.cli.setup.mandated_skills import MandatedSkillProvisioner
from teatree.harness_skills import SkillsHarness
from teatree.provisioning.skills_cli import (
    SKILLS_CLI_VERSION,
    SkillAddResult,
    SkillAddStatus,
    SkillsCliCommandError,
    SkillsCliVersionError,
)


class FakeSkillsCli:
    def __init__(
        self,
        results: tuple[SkillAddResult, ...] = (),
        error: SkillsCliCommandError | None = None,
        version_error: SkillsCliVersionError | None = None,
    ) -> None:
        self.results = results
        self.error = error
        self.version_error = version_error
        self.calls: list[tuple[str, tuple[SkillsHarness, ...], tuple[str, ...]]] = []
        self.events: list[str] = []

    def version(self) -> str:
        self.events.append("version")
        if self.version_error is not None:
            raise self.version_error
        return SKILLS_CLI_VERSION

    def add_selected(
        self,
        package: str,
        harnesses: tuple[SkillsHarness, ...],
        skills: tuple[str, ...],
    ) -> tuple[SkillAddResult, ...]:
        self.events.append("add")
        self.calls.append((package, harnesses, skills))
        if self.error is not None:
            raise self.error
        return self.results or tuple(SkillAddResult(skill, SkillAddStatus.INSTALLED) for skill in skills)


def _repo(tmp_path: Path, *dependencies: str) -> Path:
    repo = tmp_path / "teatree"
    repo.mkdir()
    lines = "\n".join(f"  - {dependency}" for dependency in dependencies)
    (repo / "apm.yml").write_text(f"name: souliane/teatree\ndependencies:\n  apm:\n{lines}\n", encoding="utf-8")
    return repo


def test_only_third_party_mandates_are_grouped_and_installed_for_both_harnesses(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "souliane/teatree/skills/architecture-design",
        "obra/superpowers/skills/writing-plans#1f20bef",
        "obra/superpowers/skills/test-driven-development#1f20bef",
        "souliane/skills/ac-python#38a0dcc",
    )
    cli = FakeSkillsCli()

    assert MandatedSkillProvisioner(repo, cli=cli).provision(lambda _line: None)
    harnesses = (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX)
    assert cli.calls == [
        ("obra/superpowers#1f20bef", harnesses, ("writing-plans", "test-driven-development")),
        ("souliane/skills#38a0dcc", harnesses, ("ac-python",)),
    ]
    assert cli.events == ["version", "add", "add"]


def test_first_party_teatree_skills_stay_in_the_plugin_lane(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "souliane/teatree/skills/architecture-design")
    cli = FakeSkillsCli()

    assert MandatedSkillProvisioner(repo, cli=cli).provision(lambda _line: None)
    assert cli.events == ["version"]
    assert cli.calls == []


def test_exact_cli_version_is_checked_before_any_install(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "souliane/skills/ac-python#38a0dcc")
    cli = FakeSkillsCli(version_error=SkillsCliVersionError("1.8.0"))
    lines: list[str] = []

    assert not MandatedSkillProvisioner(repo, cli=cli).provision(lines.append)
    assert cli.events == ["version"]
    assert cli.calls == []
    assert any("expected 1.7.0, got 1.8.0" in line for line in lines)


def test_version_mismatch_without_mandates_blocks_downstream_source_install(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "souliane/teatree/skills/architecture-design")
    cli = FakeSkillsCli(version_error=SkillsCliVersionError("1.8.0"))

    ready = MandatedSkillProvisioner(repo, cli=cli).provision(lambda _line: None)
    if ready:
        cli.add_selected("team/skills#reviewed", (SkillsHarness.CODEX,), ("runtime-demand",))

    assert ready is False
    assert cli.events == ["version"]
    assert cli.calls == []


def test_skipped_results_are_reported_as_already_installed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "souliane/skills/ac-python#38a0dcc")
    cli = FakeSkillsCli((SkillAddResult("ac-python", SkillAddStatus.SKIPPED),))
    lines: list[str] = []

    assert MandatedSkillProvisioner(repo, cli=cli).provision(lines.append)
    assert any("already installed" in line and "ac-python" in line for line in lines)


def test_failed_add_result_warns_and_returns_false(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "souliane/skills/ac-python#38a0dcc")
    cli = FakeSkillsCli((SkillAddResult("ac-python", SkillAddStatus.FAILED),))
    lines: list[str] = []

    assert not MandatedSkillProvisioner(repo, cli=cli).provision(lines.append)
    assert any(line.startswith("WARN") and "ac-python" in line for line in lines)


def test_cli_failure_warns_with_the_package_and_returns_false(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "souliane/skills/ac-python#38a0dcc")
    cli = FakeSkillsCli(error=SkillsCliCommandError(("skills", "add"), 1, "offline"))
    lines: list[str] = []

    assert not MandatedSkillProvisioner(repo, cli=cli).provision(lines.append)
    assert any(line.startswith("WARN") and "souliane/skills#38a0dcc" in line and "offline" in line for line in lines)


def test_unreadable_manifest_warns_without_calling_the_cli(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    cli = FakeSkillsCli()
    lines: list[str] = []

    assert not MandatedSkillProvisioner(repo, cli=cli).provision(lines.append)
    assert cli.events == ["version"]
    assert cli.calls == []
    assert any(line.startswith("WARN") for line in lines)
