from pathlib import Path

import pytest

from teatree.cli.setup.mandated_skills import MandatedSkillProvisioner
from teatree.harness_skills import SkillsHarness
from teatree.provisioning.skills_cli import (
    SKILLS_CLI_VERSION,
    SkillAddResult,
    SkillAddStatus,
    SkillsCliCommandError,
    SkillsCliVersionError,
)
from tests._git_repo import make_git_repo, run_git

_SHA = "1f20bef3f59b85ad7b52718f822e37c4478a3ff5"
_OTHER_SHA = "38a0dcc1e5f4b2a9d8e7f6a5b4c3d2e1f0a9b8c7"


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


@pytest.fixture
def remote(tmp_path: Path) -> str:
    for owner_repo in ("obra/superpowers", "souliane/skills"):
        make_git_repo(tmp_path / "remote" / owner_repo)
    return f"{tmp_path / 'remote'}/"


def _provision(repo: Path, cli: FakeSkillsCli, remote: str, echo=lambda _line: None) -> bool:
    return MandatedSkillProvisioner(repo, cli=cli, remote_base=remote).provision(echo)


def _name_a_ref_like_the_pin(remote: str, kind: str, name: str = _OTHER_SHA) -> None:
    source = Path(remote) / "souliane/skills"
    if kind == "annotated tag":
        run_git(source, "tag", "-a", "-m", "annotated", "scratch")
        run_git(source, "update-ref", f"refs/tags/{name}", run_git(source, "rev-parse", "refs/tags/scratch"))
    else:
        run_git(source, "update-ref", f"refs/{'heads' if kind == 'branch' else 'tags'}/{name}", "HEAD")


def _repo(tmp_path: Path, *dependencies: str) -> Path:
    repo = tmp_path / "teatree"
    repo.mkdir()
    lines = "\n".join(f"  - {dependency}" for dependency in dependencies)
    (repo / "apm.yml").write_text(f"name: souliane/teatree\ndependencies:\n  apm:\n{lines}\n", encoding="utf-8")
    return repo


_HARNESSES = (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX)


def test_only_third_party_mandates_are_grouped_and_installed_for_both_harnesses(tmp_path: Path, remote: str) -> None:
    repo = _repo(
        tmp_path,
        "souliane/teatree/skills/architecture-design",
        f"obra/superpowers/skills/writing-plans#{_SHA}",
        f"obra/superpowers/skills/test-driven-development#{_SHA}",
        f"souliane/skills/ac-python#{_OTHER_SHA}",
    )
    cli = FakeSkillsCli()

    assert _provision(repo, cli, remote)
    assert cli.calls == [
        (f"obra/superpowers#{_SHA}", _HARNESSES, ("writing-plans", "test-driven-development")),
        (f"souliane/skills#{_OTHER_SHA}", _HARNESSES, ("ac-python",)),
    ]
    assert cli.events == ["version", "add", "add"]


def test_first_party_teatree_skills_stay_in_the_plugin_lane(tmp_path: Path, remote: str) -> None:
    repo = _repo(tmp_path, "souliane/teatree/skills/architecture-design")
    cli = FakeSkillsCli()

    assert _provision(repo, cli, remote)
    assert cli.events == ["version"]
    assert cli.calls == []


def test_exact_cli_version_is_checked_before_any_install(tmp_path: Path, remote: str) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA}")
    cli = FakeSkillsCli(version_error=SkillsCliVersionError("1.8.0"))
    lines: list[str] = []

    assert not _provision(repo, cli, remote, lines.append)
    assert cli.events == ["version"]
    assert cli.calls == []
    assert any("expected 1.7.0, got 1.8.0" in line for line in lines)


def test_version_mismatch_without_mandates_blocks_downstream_source_install(tmp_path: Path, remote: str) -> None:
    repo = _repo(tmp_path, "souliane/teatree/skills/architecture-design")
    cli = FakeSkillsCli(version_error=SkillsCliVersionError("1.8.0"))

    ready = _provision(repo, cli, remote)
    if ready:
        cli.add_selected("team/skills#reviewed", (SkillsHarness.CODEX,), ("runtime-demand",))

    assert ready is False
    assert cli.events == ["version"]
    assert cli.calls == []


def test_a_skipped_result_is_not_provisioned_never_reported_as_already_installed(tmp_path: Path, remote: str) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA}")
    cli = FakeSkillsCli((SkillAddResult("ac-python", SkillAddStatus.SKIPPED),))
    lines: list[str] = []

    assert not _provision(repo, cli, remote, lines.append)
    assert any(line.startswith("WARN") and "ac-python" in line for line in lines)
    assert not any("already installed" in line for line in lines)


def test_failed_add_result_warns_and_returns_false(tmp_path: Path, remote: str) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA}")
    cli = FakeSkillsCli((SkillAddResult("ac-python", SkillAddStatus.FAILED),))
    lines: list[str] = []

    assert not _provision(repo, cli, remote, lines.append)
    assert any(line.startswith("WARN") and "ac-python" in line for line in lines)


def test_cli_failure_warns_with_the_package_and_returns_false(tmp_path: Path, remote: str) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA}")
    cli = FakeSkillsCli(error=SkillsCliCommandError(("skills", "add"), 1, "offline"))
    lines: list[str] = []

    assert not _provision(repo, cli, remote, lines.append)
    assert any(
        line.startswith("WARN") and f"souliane/skills#{_OTHER_SHA}" in line and "offline" in line for line in lines
    )


def test_unreadable_manifest_warns_without_calling_the_cli(tmp_path: Path, remote: str) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    cli = FakeSkillsCli()
    lines: list[str] = []

    assert not _provision(repo, cli, remote, lines.append)
    assert cli.events == ["version"]
    assert cli.calls == []
    assert any(line.startswith("WARN") for line in lines)


@pytest.mark.parametrize(
    "floating", ["souliane/skills/ac-python", "souliane/skills/ac-python#main", f"souliane/skills/ac-python#{_SHA[:7]}"]
)
def test_a_floating_spec_is_refused_before_the_cli_while_a_pinned_sibling_still_installs(
    tmp_path: Path, remote: str, floating: str
) -> None:
    repo = _repo(tmp_path, floating, f"obra/superpowers/skills/writing-plans#{_SHA}")
    cli = FakeSkillsCli()
    lines: list[str] = []

    assert not _provision(repo, cli, remote, lines.append)
    assert cli.calls == [(f"obra/superpowers#{_SHA}", _HARNESSES, ("writing-plans",))]
    assert any(line.startswith("WARN") and f"`{floating}`" in line and "40-hex" in line for line in lines)


def test_the_cli_receives_the_lowercase_commit_so_an_uppercase_spec_cannot_reach_an_uppercase_branch(
    tmp_path: Path, remote: str
) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA.upper()}")
    _name_a_ref_like_the_pin(remote, "branch", _OTHER_SHA.upper())
    cli = FakeSkillsCli()

    assert _provision(repo, cli, remote)
    assert cli.calls == [(f"souliane/skills#{_OTHER_SHA}", _HARNESSES, ("ac-python",))]


@pytest.mark.parametrize("kind", ["branch", "lightweight tag", "annotated tag"])
def test_a_pin_that_is_also_a_ref_name_on_the_source_is_refused_before_the_cli(
    tmp_path: Path, remote: str, kind: str
) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA}")
    _name_a_ref_like_the_pin(remote, kind)
    cli = FakeSkillsCli()
    lines: list[str] = []

    assert not _provision(repo, cli, remote, lines.append)
    assert cli.calls == []
    assert any(line.startswith("WARN") and f"/{_OTHER_SHA}" in line and "named like" in line for line in lines)


def test_an_unreachable_source_is_refused_fail_closed_and_says_the_installed_copy_is_unchanged(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA}")
    cli = FakeSkillsCli()
    lines: list[str] = []

    assert not _provision(repo, cli, f"{tmp_path / 'nowhere'}/", lines.append)
    assert cli.calls == []
    assert any("installed copy is left unchanged" in line and "private" in line for line in lines)


def test_one_unprovable_source_does_not_block_another(tmp_path: Path, remote: str) -> None:
    repo = _repo(tmp_path, f"souliane/skills/ac-python#{_OTHER_SHA}", f"obra/superpowers/skills/writing-plans#{_SHA}")
    _name_a_ref_like_the_pin(remote, "branch")
    cli = FakeSkillsCli()

    assert not _provision(repo, cli, remote)
    assert cli.calls == [(f"obra/superpowers#{_SHA}", _HARNESSES, ("writing-plans",))]
