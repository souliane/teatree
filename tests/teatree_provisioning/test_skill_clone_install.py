import subprocess
from pathlib import Path

import pytest

from teatree.harness_skills import SkillsHarness
from teatree.provisioning import skill_clone_install
from teatree.provisioning.skill_clone_install import CloneInstall, _cache_name, _export_ref, install_published_skills
from teatree.provisioning.skill_drift import PublishedSkills, SkillSourceClone
from teatree.provisioning.skills_cli import SkillAddResult, SkillAddStatus, SkillsCliCommandError
from tests._git_repo import make_git_repo, run_git


class FakeSkillsCli:
    def __init__(
        self,
        *,
        error: SkillsCliCommandError | None = None,
        results: tuple[SkillAddResult, ...] = (),
    ) -> None:
        self.error = error
        self.results = results
        self.calls: list[tuple[str, tuple[SkillsHarness, ...], tuple[str, ...]]] = []

    def add_selected(
        self,
        package: str,
        harnesses: tuple[SkillsHarness, ...],
        skills: tuple[str, ...],
    ) -> tuple[SkillAddResult, ...]:
        self.calls.append((package, harnesses, skills))
        if self.error is not None:
            raise self.error
        return (
            tuple(result for result in self.results if result.name in skills)
            if self.results
            else tuple(SkillAddResult(skill, SkillAddStatus.INSTALLED) for skill in skills)
        )


@pytest.mark.parametrize(
    ("result", "prefix"),
    [
        (CloneInstall("team", ref="main", excluded=("codex:alpha",)), "OK"),
        (CloneInstall("team", ref="main", already_loadable=("codex:alpha",)), "OK"),
        (CloneInstall("team", ref="main"), "OK"),
        (CloneInstall("team", ref="main", installed=("codex:alpha",)), "OK"),
        (CloneInstall("team", unavailable="offline"), "WARN"),
    ],
)
def test_clone_install_render_covers_every_outcome(result: CloneInstall, prefix: str) -> None:
    assert result.render().startswith(prefix)


def test_clone_install_render_caps_long_installed_lists() -> None:
    result = CloneInstall("team", installed=tuple(f"codex:skill-{index}" for index in range(10)))

    assert "(+2 more)" in result.render()


def test_export_ref_materializes_once_and_reuses_the_stamp(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "skill.txt").write_text("body", encoding="utf-8")
    run_git(repo, "add", "skill.txt")
    run_git(repo, "commit", "-q", "-m", "skill")
    destination = tmp_path / "cache" / "export"
    destination.mkdir(parents=True)
    (destination / "partial.txt").write_text("partial", encoding="utf-8")

    assert _export_ref(repo, "HEAD", destination)
    assert (destination / "skill.txt").read_text(encoding="utf-8") == "body"
    assert _export_ref(repo, "HEAD", destination)

    fresh = tmp_path / "cache" / "fresh"
    assert _export_ref(repo, "HEAD", fresh)
    assert (fresh / "skill.txt").is_file()


def test_export_ref_failure_cleans_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = subprocess.CompletedProcess(["git"], 1, "", "offline")
    monkeypatch.setattr(skill_clone_install, "run_allowed_to_fail", lambda *_args, **_kwargs: failure)
    destination = tmp_path / "cache" / "export"

    assert not _export_ref(tmp_path / "repo", "HEAD", destination)
    assert list((tmp_path / "cache").glob("export.partial.*")) == []


def test_cache_name_uses_commit_or_ref_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    success = subprocess.CompletedProcess(["git"], 0, "abc123\n", "")
    monkeypatch.setattr(skill_clone_install, "run_allowed_to_fail", lambda *_args, **_kwargs: success)
    assert _cache_name("team/skills", tmp_path, "origin/main") == "team-skills@abc123"

    failure = subprocess.CompletedProcess(["git"], 1, "", "")
    monkeypatch.setattr(skill_clone_install, "run_allowed_to_fail", lambda *_args, **_kwargs: failure)
    assert _cache_name("team skills", tmp_path, "origin/main") == "team-skills@origin-main"


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PublishedSkills:
    value = PublishedSkills(
        label="team/skills",
        repo=tmp_path / "repo",
        ref="origin/main",
        names={"skills/alpha/SKILL.md": "alpha", "skills/beta/SKILL.md": "beta"},
    )
    monkeypatch.setattr(skill_clone_install, "resolve_published_skills", lambda _clone: value)
    monkeypatch.setattr(skill_clone_install, "_cache_name", lambda *_args: "export")
    monkeypatch.setattr(skill_clone_install, "_export_ref", lambda *_args: True)
    return value


def test_only_demanded_published_skills_are_added_to_both_harnesses(
    tmp_path: Path,
    published: PublishedSkills,
) -> None:
    cli = FakeSkillsCli()

    result = install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path / "cache",
        demand_names={"alpha", "not-published"},
        harness_exclusions=[],
        cli=cli,
    )

    assert cli.calls == [
        (
            str(tmp_path / "cache" / "export"),
            (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX),
            ("alpha",),
        )
    ]
    assert result.installed == ("claude-code:alpha", "codex:alpha")


def test_per_harness_exclusions_do_not_split_required_selected_sets(
    tmp_path: Path,
    published: PublishedSkills,
) -> None:
    cli = FakeSkillsCli()

    result = install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path / "cache",
        demand_names={"alpha", "beta"},
        harness_exclusions=["claude-code:beta"],
        cli=cli,
    )

    package = str(tmp_path / "cache" / "export")
    assert cli.calls == [
        (package, (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX), ("alpha", "beta")),
    ]
    assert result.excluded == ()


def test_required_skill_is_installed_for_an_excluded_harness(
    tmp_path: Path,
    published: PublishedSkills,
) -> None:
    cli = FakeSkillsCli()

    install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path / "cache",
        demand_names={"alpha"},
        harness_exclusions=["claude-code:alpha"],
        cli=cli,
    )

    assert cli.calls == [
        (
            str(tmp_path / "cache" / "export"),
            (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX),
            ("alpha",),
        ),
    ]


def test_exclusions_cannot_disable_demanded_runtime_skills(
    tmp_path: Path,
    published: PublishedSkills,
) -> None:
    cli = FakeSkillsCli()

    install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path / "cache",
        demand_names={"Alpha"},
        harness_exclusions=["claude-code:alpha"],
        cli=cli,
    )

    assert cli.calls == [
        (
            str(tmp_path / "cache" / "export"),
            (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX),
            ("alpha",),
        ),
    ]


def test_empty_demand_does_no_source_or_cli_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_resolved(_clone: SkillSourceClone) -> PublishedSkills:
        message = "source resolution must not run"
        raise AssertionError(message)

    monkeypatch.setattr(skill_clone_install, "resolve_published_skills", fail_if_resolved)
    cli = FakeSkillsCli()

    result = install_published_skills(
        SkillSourceClone(label="team/skills"),
        cache_root=tmp_path / "cache",
        demand_names=set(),
        harness_exclusions=[],
        cli=cli,
    )

    assert cli.calls == []
    assert not (tmp_path / "cache").exists()
    assert result.installed == ()


def test_cli_failure_is_rendered_as_unavailable(tmp_path: Path, published: PublishedSkills) -> None:
    cli = FakeSkillsCli(error=SkillsCliCommandError(("skills", "add"), 1, "offline"))

    result = install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path / "cache",
        demand_names={"alpha"},
        harness_exclusions=[],
        cli=cli,
    )

    assert "offline" in result.unavailable
    assert result.render().startswith("WARN")


def test_unmeasurable_source_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        skill_clone_install,
        "resolve_published_skills",
        lambda _clone: PublishedSkills(label="team", unmeasurable="missing clone"),
    )

    result = install_published_skills(
        SkillSourceClone(label="team"),
        cache_root=tmp_path,
        demand_names={"alpha"},
        harness_exclusions=[],
        cli=FakeSkillsCli(),
    )

    assert result.unavailable == "missing clone"


def test_source_with_no_matching_demand_does_not_export(
    tmp_path: Path,
    published: PublishedSkills,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        skill_clone_install,
        "_export_ref",
        lambda *_args: pytest.fail("export must not run"),
    )

    result = install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path,
        demand_names={"other"},
        harness_exclusions=[],
        cli=FakeSkillsCli(),
    )

    assert result.installed == ()


def test_export_failure_is_unavailable(
    tmp_path: Path,
    published: PublishedSkills,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_clone_install, "_export_ref", lambda *_args: False)

    result = install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path,
        demand_names={"alpha"},
        harness_exclusions=[],
        cli=FakeSkillsCli(),
    )

    assert "could not export" in result.unavailable


def test_skipped_and_failed_add_results_are_not_reported_as_installed(
    tmp_path: Path,
    published: PublishedSkills,
) -> None:
    cli = FakeSkillsCli(
        results=(
            SkillAddResult("alpha", SkillAddStatus.SKIPPED),
            SkillAddResult("beta", SkillAddStatus.FAILED),
        )
    )

    result = install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path / "cache",
        demand_names={"alpha", "beta"},
        harness_exclusions=["claude-code:alpha"],
        cli=cli,
    )

    assert result.already_loadable == ("claude-code:alpha", "codex:alpha")
    assert "beta" in result.unavailable


def test_default_cli_factory_is_used_when_not_injected(
    tmp_path: Path,
    published: PublishedSkills,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = FakeSkillsCli()
    monkeypatch.setattr(skill_clone_install, "SkillsCli", lambda: cli)

    result = install_published_skills(
        SkillSourceClone(label=published.label),
        cache_root=tmp_path / "cache",
        demand_names={"alpha"},
        harness_exclusions=[],
    )

    assert result.installed == ("claude-code:alpha", "codex:alpha")


def test_empty_label_gets_a_readable_no_work_result(tmp_path: Path) -> None:
    result = install_published_skills(
        SkillSourceClone(),
        cache_root=tmp_path,
        demand_names=set(),
        harness_exclusions=[],
        cli=FakeSkillsCli(),
    )

    assert result.label == "skill source"
