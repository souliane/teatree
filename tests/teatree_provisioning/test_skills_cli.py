import fcntl
import json
import shutil
import subprocess
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import teatree.provisioning.skills_cli as skills_cli_module
from teatree.provisioning.skill_provenance import SkillInstallationFact, SkillInstallKind
from teatree.provisioning.skills_cli import (
    SKILLS_CLI_VERSION,
    SKILLS_RECEIPT_VERSION,
    HarnessSkillInventory,
    InstalledSkill,
    SkillAddResult,
    SkillAddStatus,
    SkillsCli,
    SkillsCliCommandError,
    SkillsCliInputError,
    SkillsCliSchemaError,
    SkillsCliUnavailableError,
    SkillsCliVersionError,
    SkillsHarness,
    SkillsInventoryReceipt,
    SkillsInventoryReceiptError,
    UnsupportedSkillsHarnessError,
    read_inventory_receipt,
    refresh_inventory_receipt,
)


class RecordedRunner:
    def __init__(self, *responses: subprocess.CompletedProcess[str]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(command))
        return self.responses.popleft()


class RecordedProbe:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def probe(self, path: Path) -> SkillInstallationFact:
        self.paths.append(path)
        return SkillInstallationFact(
            front_door_path=str(path),
            kind=SkillInstallKind.MISSING,
            link_target=None,
            git=None,
        )


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=("skills",), returncode=returncode, stdout=stdout, stderr=stderr)


def test_version_and_list_use_exact_installed_cli_contract(tmp_path: Path) -> None:
    payload = [
        {
            "name": "review",
            "path": "/home/teatree/.claude/skills/review",
            "scope": "global",
            "agents": ["Claude Code"],
            "source": "souliane/teatree",
            "sourceUrl": "https://github.com/souliane/teatree.git",
            "sourceType": "github",
        },
        {
            "name": "local-skill",
            "path": "/home/teatree/.claude/skills/local-skill",
            "scope": "global",
            "agents": [],
            "source": None,
            "sourceUrl": None,
            "sourceType": None,
        },
    ]
    home = tmp_path / "home"
    for name in ("review", "local-skill"):
        (home / ".claude" / "skills" / name).mkdir(parents=True)
    runner = RecordedRunner(
        _completed(f"{SKILLS_CLI_VERSION}\n", stderr="upgrade warning\n"),
        _completed(json.dumps(payload), stderr="yaml warning\n"),
    )
    cli = SkillsCli(runner=runner)

    assert cli.version() == SKILLS_CLI_VERSION
    assert cli.list_global(SkillsHarness.CLAUDE_CODE, home=home) == (
        InstalledSkill(
            name="review",
            path="/home/teatree/.claude/skills/review",
            scope="global",
            agents=("Claude Code",),
            source="souliane/teatree",
            source_url="https://github.com/souliane/teatree.git",
            source_type="github",
        ),
        InstalledSkill(
            name="local-skill",
            path="/home/teatree/.claude/skills/local-skill",
            scope="global",
            agents=(),
            source=None,
            source_url=None,
            source_type=None,
        ),
    )
    assert runner.calls == [
        ("skills", "--version"),
        ("skills", "list", "-g", "-a", "claude-code", "--json"),
    ]


@pytest.mark.parametrize("error_type", [FileNotFoundError, NotADirectoryError])
def test_missing_binary_is_an_actionable_typed_error(error_type: type[OSError]) -> None:
    def missing_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise error_type(command[0])

    with pytest.raises(SkillsCliUnavailableError, match=r"skills@1\.7\.0") as error:
        SkillsCli(runner=missing_runner).version()

    assert error.value.command == ("skills", "--version")


def test_command_failure_preserves_command_status_and_stderr() -> None:
    runner = RecordedRunner(_completed(stderr="repository unavailable", returncode=7))

    with pytest.raises(SkillsCliCommandError, match="repository unavailable") as error:
        SkillsCli(runner=runner).version()

    assert error.value.command == ("skills", "--version")
    assert error.value.returncode == 7
    assert error.value.stderr == "repository unavailable"


def test_version_rejects_a_drifted_cli() -> None:
    with pytest.raises(SkillsCliVersionError) as error:
        SkillsCli(runner=RecordedRunner(_completed("1.8.0\n"))).version()

    assert error.value.expected == SKILLS_CLI_VERSION
    assert error.value.actual == "1.8.0"


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "[not-json",
        json.dumps({"name": "not-a-list"}),
        json.dumps(["not-an-object"]),
        json.dumps([{"name": "missing-fields"}]),
        json.dumps(
            [
                {
                    "name": "wrong-scope",
                    "path": "/tmp/wrong-scope",
                    "scope": "project",
                    "agents": [],
                    "source": None,
                    "sourceUrl": None,
                    "sourceType": None,
                }
            ]
        ),
        json.dumps(
            [
                {
                    "name": "wrong-agents",
                    "path": "/tmp/wrong-agents",
                    "scope": "global",
                    "agents": "Claude Code",
                    "source": None,
                    "sourceUrl": None,
                    "sourceType": None,
                }
            ]
        ),
        json.dumps(
            [
                {
                    "name": "wrong-agent-entry",
                    "path": "/tmp/wrong-agent-entry",
                    "scope": "global",
                    "agents": [7],
                    "source": None,
                    "sourceUrl": None,
                    "sourceType": None,
                }
            ]
        ),
        json.dumps(
            [
                {
                    "name": "",
                    "path": "/tmp/empty-name",
                    "scope": "global",
                    "agents": [],
                    "source": None,
                    "sourceUrl": None,
                    "sourceType": None,
                }
            ]
        ),
        json.dumps(
            [
                {
                    "name": "wrong-source",
                    "path": "/tmp/wrong-source",
                    "scope": "global",
                    "agents": [],
                    "source": 7,
                    "sourceUrl": None,
                    "sourceType": None,
                }
            ]
        ),
    ],
)
def test_list_rejects_invalid_json_schema(stdout: str) -> None:
    cli = SkillsCli(runner=RecordedRunner(_completed(stdout, stderr="warning that is not JSON")))
    with pytest.raises(SkillsCliSchemaError):
        cli.list_global(SkillsHarness.CODEX)


def test_list_rejects_an_unsupported_harness_even_if_typing_is_bypassed() -> None:
    unsupported = cast("SkillsHarness", "cursor")
    with pytest.raises(UnsupportedSkillsHarnessError, match="unsupported skills harness"):
        SkillsCli(runner=RecordedRunner()).list_global(unsupported)


def test_default_runner_uses_subprocess_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(
        command: Sequence[str],
        *,
        expected_codes: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(command), {"expected_codes": expected_codes}))
        return _completed(f"{SKILLS_CLI_VERSION}\n")

    monkeypatch.setattr(skills_cli_module, "run_allowed_to_fail", fake_run)

    assert SkillsCli().version() == SKILLS_CLI_VERSION
    assert calls == [
        (
            ("skills", "--version"),
            {"expected_codes": None},
        )
    ]


def test_command_failure_without_stderr_remains_actionable() -> None:
    with pytest.raises(SkillsCliCommandError, match="no stderr"):
        SkillsCli(runner=RecordedRunner(_completed(returncode=1))).version()


def test_empty_version_output_is_reported_explicitly() -> None:
    with pytest.raises(SkillsCliVersionError, match="empty output"):
        SkillsCli(runner=RecordedRunner(_completed())).version()


def test_remove_targets_one_concrete_name_and_harness(tmp_path: Path) -> None:
    runner = RecordedRunner(_completed())

    SkillsCli(runner=runner).remove_global("review", SkillsHarness.CLAUDE_CODE, home=tmp_path)

    assert runner.calls == [
        ("skills", "remove", "review", "-g", "-a", "claude-code", "-y"),
    ]


def test_list_classifies_harness_presence_from_real_front_doors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude_only = home / ".claude" / "skills" / "claude-only"
    universal = home / ".agents" / "skills" / "both"
    legacy = home / ".codex" / "skills" / "both"
    claude_only.mkdir(parents=True)
    universal.mkdir(parents=True)
    legacy.mkdir(parents=True)
    (home / ".claude" / "skills" / "both").symlink_to(universal, target_is_directory=True)
    rows = [
        _installed_payload_at("claude-only", claude_only, ["Claude Code"]),
        _installed_payload_at("both", universal, ["Claude Code"]),
        _installed_payload_at("both", legacy, []),
    ]
    runner = RecordedRunner(_completed(json.dumps(rows)), _completed(json.dumps(rows)))
    cli = SkillsCli(runner=runner)

    assert [skill.path for skill in cli.list_global(SkillsHarness.CLAUDE_CODE, home=home)] == [
        str(claude_only),
        str(universal),
    ]
    assert [skill.path for skill in cli.list_global(SkillsHarness.CODEX, home=home)] == [str(universal), str(legacy)]


def test_list_accepts_prefixed_warnings_before_the_final_json_value(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".agents" / "skills" / "review"
    path.mkdir(parents=True)
    payload = json.dumps([_installed_payload_at("review", path, [])])
    stdout = f"Installing skills\nYAML warning: ignored field\n{payload}\n"

    skills = SkillsCli(runner=RecordedRunner(_completed(stdout))).list_global(SkillsHarness.CODEX, home=home)

    assert [skill.name for skill in skills] == ["review"]


def test_remove_codex_preserves_claude_as_a_copy_and_deletes_universal_front_door(tmp_path: Path) -> None:
    home = tmp_path / "home"
    universal = home / ".agents" / "skills" / "review"
    universal.mkdir(parents=True)
    (universal / "SKILL.md").write_text("body", encoding="utf-8")
    claude = home / ".claude" / "skills" / "review"
    claude.parent.mkdir(parents=True)
    claude.symlink_to(universal, target_is_directory=True)
    runner = RecordedRunner(_completed())

    SkillsCli(runner=runner).remove_global("review", SkillsHarness.CODEX, home=home)

    assert runner.calls == [("skills", "remove", "review", "-g", "-a", "codex", "-y")]
    assert not universal.exists()
    assert not claude.is_symlink()
    assert (claude / "SKILL.md").read_text(encoding="utf-8") == "body"


def test_remove_codex_deletes_universal_and_legacy_front_doors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    universal = home / ".agents" / "skills" / "review"
    legacy = home / ".codex" / "skills" / "review"
    universal.mkdir(parents=True)
    legacy.mkdir(parents=True)

    SkillsCli(runner=RecordedRunner(_completed())).remove_global("review", SkillsHarness.CODEX, home=home)

    assert not universal.exists()
    assert not legacy.exists()


def test_remove_codex_unlinks_front_doors_without_deleting_their_targets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    universal = home / ".agents" / "skills" / "review"
    universal.parent.mkdir(parents=True)
    universal.symlink_to(target, target_is_directory=True)
    legacy = home / ".codex" / "skills" / "review"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")

    SkillsCli(runner=RecordedRunner(_completed())).remove_global("review", SkillsHarness.CODEX, home=home)

    assert not universal.is_symlink()
    assert target.is_dir()
    assert not legacy.exists()


def test_remove_codex_keeps_an_independent_claude_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    universal = home / ".agents" / "skills" / "review"
    universal.mkdir(parents=True)
    independent = tmp_path / "independent"
    independent.mkdir()
    claude = home / ".claude" / "skills" / "review"
    claude.parent.mkdir(parents=True)
    claude.symlink_to(independent, target_is_directory=True)

    SkillsCli(runner=RecordedRunner(_completed())).remove_global("review", SkillsHarness.CODEX, home=home)

    assert claude.is_symlink()
    assert claude.resolve() == independent


def test_remove_codex_fails_before_cli_when_claude_copy_cannot_be_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    universal = home / ".agents" / "skills" / "review"
    universal.mkdir(parents=True)
    claude = home / ".claude" / "skills" / "review"
    claude.parent.mkdir(parents=True)
    claude.symlink_to(universal, target_is_directory=True)
    runner = RecordedRunner()

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        message = "copy refused"
        raise OSError(message)

    monkeypatch.setattr(skills_cli_module.shutil, "copytree", fail_copy)

    with pytest.raises(skills_cli_module.SkillsCliRemovalError, match="preserve"):
        SkillsCli(runner=runner).remove_global("review", SkillsHarness.CODEX, home=home)

    assert runner.calls == []
    assert universal.is_dir()
    assert claude.is_symlink()


def test_remove_codex_fails_before_cli_when_claude_copy_cannot_be_prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    universal = home / ".agents" / "skills" / "review"
    universal.mkdir(parents=True)
    claude = home / ".claude" / "skills" / "review"
    claude.parent.mkdir(parents=True)
    claude.symlink_to(universal, target_is_directory=True)
    runner = RecordedRunner()
    original_resolve = Path.resolve

    def fail_claude_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == claude:
            message = "resolve refused"
            raise OSError(message)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_claude_resolve)

    with pytest.raises(skills_cli_module.SkillsCliRemovalError, match="prepare"):
        SkillsCli(runner=runner).remove_global("review", SkillsHarness.CODEX, home=home)

    assert runner.calls == []


def test_remove_reports_front_door_cleanup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    universal = home / ".agents" / "skills" / "review"
    universal.mkdir(parents=True)

    def fail_remove(_path: Path) -> None:
        message = "cleanup refused"
        raise OSError(message)

    monkeypatch.setattr(skills_cli_module, "_remove_path", fail_remove)

    with pytest.raises(skills_cli_module.SkillsCliRemovalError, match="cleanup failed"):
        SkillsCli(runner=RecordedRunner(_completed())).remove_global("review", SkillsHarness.CODEX, home=home)


def test_remove_reports_when_cli_leaves_claude_front_door_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "review").mkdir(parents=True)

    with pytest.raises(skills_cli_module.SkillsCliRemovalError, match="still present"):
        SkillsCli(runner=RecordedRunner(_completed())).remove_global("review", SkillsHarness.CLAUDE_CODE, home=home)


@pytest.mark.parametrize("scenario", ["claude-only", "both"])
def test_remove_claude_deletes_only_its_front_door(tmp_path: Path, scenario: str) -> None:
    home = tmp_path / "home"
    claude = home / ".claude" / "skills" / "review"
    claude.mkdir(parents=True)
    universal = home / ".agents" / "skills" / "review"
    if scenario == "both":
        universal.mkdir(parents=True)

    def runner(_command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        claude.rmdir()
        return _completed()

    SkillsCli(runner=runner).remove_global("review", SkillsHarness.CLAUDE_CODE, home=home)

    assert not claude.exists()
    assert universal.exists() is (scenario == "both")


def test_remove_claude_restores_shared_codex_front_door_if_cli_deletes_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    universal = home / ".agents" / "skills" / "review"
    universal.mkdir(parents=True)
    (universal / "SKILL.md").write_text("codex copy", encoding="utf-8")
    claude = home / ".claude" / "skills" / "review"
    claude.parent.mkdir(parents=True)
    claude.symlink_to(universal, target_is_directory=True)

    def runner(_command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        claude.unlink()
        shutil.rmtree(universal)
        return _completed()

    SkillsCli(runner=runner).remove_global("review", SkillsHarness.CLAUDE_CODE, home=home)

    assert not claude.exists()
    assert (universal / "SKILL.md").read_text(encoding="utf-8") == "codex copy"


@pytest.mark.parametrize(
    "name",
    ["", "   ", "*", "--all", "-dangerous", ".", "..", "../review", "foo/bar", r"foo\bar"],
)
def test_remove_rejects_non_concrete_names_without_invoking_cli(name: str) -> None:
    runner = RecordedRunner()
    with pytest.raises(SkillsCliInputError, match="concrete skill name"):
        SkillsCli(runner=runner).remove_global(name, SkillsHarness.CODEX)
    assert runner.calls == []


def test_add_selected_targets_explicit_harnesses_and_skills() -> None:
    runner = RecordedRunner(
        _completed(
            json.dumps(
                [
                    {"name": "review", "status": "installed"},
                    {"name": "test", "status": "skipped"},
                ]
            )
        )
    )

    results = SkillsCli(runner=runner).add_selected(
        "souliane/teatree",
        (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX),
        ("review", "test"),
    )

    assert results == (
        SkillAddResult(name="review", status=SkillAddStatus.INSTALLED),
        SkillAddResult(name="test", status=SkillAddStatus.SKIPPED),
    )
    assert runner.calls == [
        (
            "skills",
            "add",
            "souliane/teatree",
            "-g",
            "-a",
            "claude-code",
            "codex",
            "-s",
            "review",
            "test",
            "-y",
            "--json",
        )
    ]
    assert "--all" not in runner.calls[0]
    assert "*" not in runner.calls[0]


def test_add_selected_for_claude_only_forces_a_private_copy() -> None:
    runner = RecordedRunner(_completed('[{"name":"review","status":"installed"}]'))

    SkillsCli(runner=runner).add_selected("team/skills", (SkillsHarness.CLAUDE_CODE,), ("review",))

    assert "--copy" in runner.calls[0]


def test_add_selected_accepts_prefixed_output_before_the_final_json_array() -> None:
    stdout = 'Installing skills\nYAML warning: ignored field\n[{"name":"review","status":"installed"}]\n'

    result = SkillsCli(runner=RecordedRunner(_completed(stdout))).add_selected(
        "team/skills", (SkillsHarness.CODEX,), ("review",)
    )

    assert result == (SkillAddResult(name="review", status=SkillAddStatus.INSTALLED),)


@pytest.mark.parametrize("package", ["", "   ", "*", "--all", "-dangerous"])
def test_add_selected_rejects_non_concrete_packages(package: str) -> None:
    runner = RecordedRunner()
    with pytest.raises(SkillsCliInputError, match="concrete package"):
        SkillsCli(runner=runner).add_selected(
            package,
            (SkillsHarness.CODEX,),
            ("review",),
        )
    assert runner.calls == []


@pytest.mark.parametrize("skills", [(), ("",), ("*",), ("--all",), ("-dangerous",)])
def test_add_selected_requires_nonempty_concrete_skill_names(skills: tuple[str, ...]) -> None:
    runner = RecordedRunner()
    with pytest.raises(SkillsCliInputError, match="selected skills"):
        SkillsCli(runner=runner).add_selected(
            "souliane/teatree",
            (SkillsHarness.CODEX,),
            skills,
        )
    assert runner.calls == []


def test_add_selected_requires_at_least_one_harness() -> None:
    runner = RecordedRunner()
    with pytest.raises(SkillsCliInputError, match="harness"):
        SkillsCli(runner=runner).add_selected("souliane/teatree", (), ("review",))
    assert runner.calls == []


@pytest.mark.parametrize(
    "stdout",
    [
        json.dumps({"name": "review", "status": "installed"}),
        json.dumps(["review"]),
        json.dumps([{"name": "review", "status": "unknown"}]),
    ],
)
def test_add_selected_rejects_invalid_result_schema(stdout: str) -> None:
    cli = SkillsCli(runner=RecordedRunner(_completed(stdout)))
    with pytest.raises(SkillsCliSchemaError):
        cli.add_selected(
            "souliane/teatree",
            (SkillsHarness.CODEX,),
            ("review",),
        )


def _installed_payload_at(name: str, path: Path, agents: list[str]) -> dict[str, object]:
    return {
        "name": name,
        "path": str(path),
        "scope": "global",
        "agents": agents,
        "source": "team/skills",
        "sourceUrl": "https://github.com/team/skills.git",
        "sourceType": "github",
    }


def test_refresh_enriches_and_round_trips_actual_harness_front_doors(tmp_path: Path) -> None:
    target = tmp_path / "skills-inventory.json"
    home = tmp_path / "home"
    runner = RecordedRunner(
        _completed(f"{SKILLS_CLI_VERSION}\n"),
        _completed(json.dumps([_installed_payload_at("review", home / ".claude" / "skills" / "review", [])])),
        _completed(json.dumps([_installed_payload_at("test", home / ".agents" / "skills" / "test", [])])),
    )
    probe = RecordedProbe()
    (home / ".claude" / "skills" / "review").mkdir(parents=True)
    (home / ".agents" / "skills" / "test").mkdir(parents=True)

    receipt = refresh_inventory_receipt(
        target,
        cli=SkillsCli(runner=runner),
        generated_at=datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
        home=home,
        provenance_probe=probe,
    )

    expected_paths = [
        home / ".claude" / "skills" / "review",
        home / ".agents" / "skills" / "test",
    ]
    assert probe.paths == expected_paths
    assert [
        skill.installation.front_door_path
        for inventory in receipt.inventories
        for skill in inventory.skills
        if skill.installation is not None
    ] == [str(path) for path in expected_paths]
    assert read_inventory_receipt(target) == receipt
    persisted = json.loads(target.read_text())
    assert persisted["inventories"]["claude-code"][0]["installation"] == {
        "frontDoorPath": str(expected_paths[0]),
        "git": None,
        "kind": "missing",
        "linkTarget": None,
    }


def test_refresh_writes_and_reads_a_versioned_atomic_inventory_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "skills-inventory.json"
    home = tmp_path / "home"
    generated_at = datetime(2026, 9, 22, 9, 30, tzinfo=UTC)
    claude_payload = [_installed_payload_at("review", home / ".claude" / "skills" / "review", ["Claude Code"])]
    codex_payload = [_installed_payload_at("test", home / ".agents" / "skills" / "test", [])]
    runner = RecordedRunner(
        _completed(f"{SKILLS_CLI_VERSION}\n"),
        _completed(json.dumps(claude_payload), stderr="yaml warning\n"),
        _completed(json.dumps(codex_payload)),
    )
    replacements: list[tuple[Path, Path]] = []
    (home / ".claude" / "skills" / "review").mkdir(parents=True)
    (home / ".agents" / "skills" / "test").mkdir(parents=True)
    real_replace = Path.replace

    def recording_replace(source: Path, destination: str | Path) -> Path:
        replacements.append((source, Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", recording_replace)

    receipt = refresh_inventory_receipt(
        target,
        cli=SkillsCli(runner=runner),
        generated_at=generated_at,
        home=home,
    )

    assert receipt == SkillsInventoryReceipt(
        schema_version=SKILLS_RECEIPT_VERSION,
        generated_at=generated_at,
        cli_version=SKILLS_CLI_VERSION,
        inventories=(
            HarnessSkillInventory(
                harness=SkillsHarness.CLAUDE_CODE,
                skills=(
                    InstalledSkill(
                        name="review",
                        path=str(home / ".claude" / "skills" / "review"),
                        scope="global",
                        agents=("Claude Code",),
                        source="team/skills",
                        source_url="https://github.com/team/skills.git",
                        source_type="github",
                        installation=SkillInstallationFact(
                            front_door_path=str(home / ".claude" / "skills" / "review"),
                            kind=SkillInstallKind.COPY,
                            link_target=None,
                            git=None,
                        ),
                    ),
                ),
            ),
            HarnessSkillInventory(
                harness=SkillsHarness.CODEX,
                skills=(
                    InstalledSkill(
                        name="test",
                        path=str(home / ".agents" / "skills" / "test"),
                        scope="global",
                        agents=(),
                        source="team/skills",
                        source_url="https://github.com/team/skills.git",
                        source_type="github",
                        installation=SkillInstallationFact(
                            front_door_path=str(home / ".agents" / "skills" / "test"),
                            kind=SkillInstallKind.COPY,
                            link_target=None,
                            git=None,
                        ),
                    ),
                ),
            ),
        ),
    )
    assert read_inventory_receipt(target) == receipt
    assert runner.calls == [
        ("skills", "--version"),
        ("skills", "list", "-g", "-a", "claude-code", "--json"),
        ("skills", "list", "-g", "-a", "codex", "--json"),
    ]
    assert len(replacements) == 1
    assert replacements[0][1] == target
    assert not replacements[0][0].exists()
    assert json.loads(target.read_text()) == {
        "cliVersion": SKILLS_CLI_VERSION,
        "generatedAt": "2026-09-22T09:30:00+00:00",
        "inventories": {
            "claude-code": [
                {
                    **claude_payload[0],
                    "installation": {
                        "frontDoorPath": str(home / ".claude" / "skills" / "review"),
                        "git": None,
                        "kind": "copy",
                        "linkTarget": None,
                    },
                }
            ],
            "codex": [
                {
                    **codex_payload[0],
                    "installation": {
                        "frontDoorPath": str(home / ".agents" / "skills" / "test"),
                        "git": None,
                        "kind": "copy",
                        "linkTarget": None,
                    },
                }
            ],
        },
        "schemaVersion": SKILLS_RECEIPT_VERSION,
    }


def test_failed_refresh_preserves_the_last_good_receipt(tmp_path: Path) -> None:
    target = tmp_path / "skills-inventory.json"
    runner = RecordedRunner(
        _completed(f"{SKILLS_CLI_VERSION}\n"),
        _completed("[]"),
        _completed("[]"),
        _completed(f"{SKILLS_CLI_VERSION}\n"),
        _completed("[]"),
        _completed(stderr="codex inventory failed", returncode=9),
    )
    cli = SkillsCli(runner=runner)
    refresh_inventory_receipt(
        target,
        cli=cli,
        generated_at=datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
    )
    last_good = target.read_bytes()

    with pytest.raises(SkillsCliCommandError, match="codex inventory failed"):
        refresh_inventory_receipt(
            target,
            cli=cli,
            generated_at=datetime(2026, 9, 22, 9, 31, tzinfo=UTC),
        )

    assert target.read_bytes() == last_good


def test_refresh_holds_an_interprocess_lock_through_probe_and_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "skills-inventory.json"
    events: list[tuple[str, object]] = []
    responses = deque(
        [
            _completed(f"{SKILLS_CLI_VERSION}\n"),
            _completed("[]"),
            _completed("[]"),
        ]
    )

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        events.append(("cli", tuple(command)))
        return responses.popleft()

    def flock(_descriptor: object, operation: int) -> None:
        events.append(("lock", operation))

    real_replace = Path.replace

    def replace(source: Path, destination: str | Path) -> Path:
        events.append(("replace", Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(Path, "replace", replace)

    refresh_inventory_receipt(
        target,
        cli=SkillsCli(runner=runner),
        generated_at=datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
    )

    assert events == [
        ("lock", fcntl.LOCK_EX),
        ("cli", ("skills", "--version")),
        ("cli", ("skills", "list", "-g", "-a", "claude-code", "--json")),
        ("cli", ("skills", "list", "-g", "-a", "codex", "--json")),
        ("replace", target),
        ("lock", fcntl.LOCK_UN),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({}),
        json.dumps(
            {
                "schemaVersion": 999,
                "generatedAt": "2026-09-22T09:30:00+00:00",
                "cliVersion": SKILLS_CLI_VERSION,
                "inventories": {"claude-code": [], "codex": []},
            }
        ),
    ],
)
def test_read_rejects_invalid_receipt_schema(tmp_path: Path, payload: str) -> None:
    target = tmp_path / "skills-inventory.json"
    target.write_text(payload)

    with pytest.raises(SkillsInventoryReceiptError):
        read_inventory_receipt(target)


def _empty_receipt_payload() -> dict[str, object]:
    return {
        "schemaVersion": SKILLS_RECEIPT_VERSION,
        "generatedAt": "2026-09-22T09:30:00+00:00",
        "cliVersion": SKILLS_CLI_VERSION,
        "inventories": {"claude-code": [], "codex": []},
    }


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {**_empty_receipt_payload(), "generatedAt": None},
        {**_empty_receipt_payload(), "generatedAt": "not-a-timestamp"},
        {**_empty_receipt_payload(), "generatedAt": "2026-09-22T09:30:00"},
        {**_empty_receipt_payload(), "cliVersion": "1.8.0"},
        {**_empty_receipt_payload(), "inventories": []},
        {**_empty_receipt_payload(), "inventories": {"codex": []}},
        {
            **_empty_receipt_payload(),
            "inventories": {"claude-code": "not-a-list", "codex": []},
        },
        {
            **_empty_receipt_payload(),
            "inventories": {"claude-code": ["not-a-skill"], "codex": []},
        },
    ],
)
def test_read_rejects_each_invalid_receipt_field(tmp_path: Path, payload: object) -> None:
    target = tmp_path / "skills-inventory.json"
    target.write_text(json.dumps(payload))

    with pytest.raises(SkillsInventoryReceiptError):
        read_inventory_receipt(target)


def test_refresh_rejects_a_naive_generated_time_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "skills-inventory.json"
    runner = RecordedRunner(
        _completed(f"{SKILLS_CLI_VERSION}\n"),
        _completed("[]"),
        _completed("[]"),
    )
    generated_at = datetime(2026, 9, 22, 9, 30, tzinfo=UTC).replace(tzinfo=None)

    with pytest.raises(SkillsInventoryReceiptError, match="timezone"):
        refresh_inventory_receipt(
            target,
            cli=SkillsCli(runner=runner),
            generated_at=generated_at,
        )

    assert not target.exists()


def test_refresh_uses_the_default_client_and_current_utc_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SkillsCli(
        runner=RecordedRunner(
            _completed(f"{SKILLS_CLI_VERSION}\n"),
            _completed("[]"),
            _completed("[]"),
        )
    )
    monkeypatch.setattr(skills_cli_module, "SkillsCli", lambda: client)

    receipt = refresh_inventory_receipt(tmp_path / "skills-inventory.json")

    assert receipt.generated_at.tzinfo is not None


def test_atomic_write_failure_cleans_the_staged_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "skills-inventory.json"
    client = SkillsCli(
        runner=RecordedRunner(
            _completed(f"{SKILLS_CLI_VERSION}\n"),
            _completed("[]"),
            _completed("[]"),
        )
    )

    def fail_replace(_source: Path, _destination: str | Path) -> Path:
        message = "replace failed"
        raise OSError(message)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        refresh_inventory_receipt(
            target,
            cli=client,
            generated_at=datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
        )

    assert not target.exists()
    assert list(tmp_path.glob(f".{target.name}-*.tmp")) == []
