import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from teatree.dash.skill_control import (
    SkillDashboardSource,
    build_skill_dashboard,
    filter_skill_dashboard,
    load_cached_receipt,
)
from teatree.harness_skills import SkillsHarness
from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.skill_provenance import GitProbeState, GitProvenance, SkillInstallationFact, SkillInstallKind
from teatree.provisioning.skills_cli import (
    SKILLS_CLI_VERSION,
    SKILLS_RECEIPT_VERSION,
    HarnessSkillInventory,
    InstalledSkill,
    SkillsInventoryReceipt,
)
from teatree.skill_support.demands import SkillDemand
from teatree.skill_support.inventory import (
    EmbeddedSkillClosure,
    SkillDeclaration,
    SkillIdentity,
    SkillInventory,
    SkillInventoryDiagnostic,
    SkillInventoryDiagnosticCode,
)


def _identity(name: str) -> SkillIdentity:
    return SkillIdentity("t3", name)


def _inventory(tmp_path: Path) -> SkillInventory:
    code = _identity("code")
    test = _identity("test")
    declarations = (
        SkillDeclaration(code, tmp_path / "code" / "SKILL.md", ("test",), (test,), (test,)),
        SkillDeclaration(test, tmp_path / "test" / "SKILL.md", (), (), ()),
    )
    agent = EmbeddedSkillClosure("coder", (code,), (test,), (test, code), ())
    phase = EmbeddedSkillClosure("coding", (code,), (test,), (test, code), ())
    return SkillInventory(declarations, (), (), (agent,), (phase,))


def _installed(name: str, harness: SkillsHarness) -> InstalledSkill:
    harness_dir = ".claude" if harness is SkillsHarness.CLAUDE_CODE else ".codex"
    return InstalledSkill(
        name=name,
        path=f"/manager/{name}",
        scope="global",
        agents=(harness.value,),
        source="acme/skills",
        source_url="https://example.invalid/acme/skills.git",
        source_type="github",
        installation=SkillInstallationFact(
            front_door_path=f"/home/operator/{harness_dir}/skills/{name}",
            kind=SkillInstallKind.COPY,
            link_target=None,
            git=None,
        ),
    )


def _receipt(*skills: tuple[SkillsHarness, InstalledSkill]) -> SkillsInventoryReceipt:
    by_harness = {harness: [] for harness in SkillsHarness}
    for harness, skill in skills:
        by_harness[harness].append(skill)
    return SkillsInventoryReceipt(
        schema_version=SKILLS_RECEIPT_VERSION,
        generated_at=datetime(2026, 9, 22, 8, 0, tzinfo=UTC),
        cli_version=SKILLS_CLI_VERSION,
        inventories=tuple(HarnessSkillInventory(harness, tuple(by_harness[harness])) for harness in SkillsHarness),
    )


def test_projection_keeps_canonical_identities_and_marks_both_collision_rows_red(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    optional_installation = SkillInstallationFact(
        "/home/.codex/skills/optional",
        SkillInstallKind.SYMLINK,
        "/src/optional",
        GitProvenance(
            state=GitProbeState.KNOWN,
            repo_path="/src",
            local_sha="1" * 40,
            branch="feature",
            detached=False,
            default_branch="main",
            default_sha="2" * 40,
            ahead=0,
            behind=2,
            newer_on_default=True,
            diagnostic=None,
        ),
    )
    receipt = _receipt(
        (SkillsHarness.CLAUDE_CODE, _installed("Code", SkillsHarness.CLAUDE_CODE)),
        (SkillsHarness.CODEX, _installed("writing-plans", SkillsHarness.CODEX)),
        (
            SkillsHarness.CODEX,
            replace(
                _installed("optional", SkillsHarness.CODEX),
                installation=optional_installation,
            ),
        ),
    )
    dependencies = (
        DeclaredDependency("skill", "writing-plans", "apm.yml", "install", "obra/superpowers/writing-plans#abc"),
    )

    dashboard = build_skill_dashboard(
        source=SkillDashboardSource(
            inventory, dependencies, (SkillDemand("review_skill", "review"),), ["codex:optional"]
        ),
        receipt=receipt,
        receipt_error=None,
        now=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    )

    code = next(row for row in dashboard.teatree_skills if row.identity == "t3:code")
    collision = next(row for row in dashboard.harnesses[0].skills if row.name == "Code")
    required = next(row for row in dashboard.harnesses[1].skills if row.name == "writing-plans")
    optional = next(row for row in dashboard.harnesses[1].skills if row.name == "optional")
    assert code.source_path == str(tmp_path / "code" / "SKILL.md")
    assert code.direct_requires == ("t3:test",)
    assert code.transitive_requires == ("t3:test",)
    assert code.direct_agents == ("coder",)
    assert code.companion_agents == ()
    assert code.embedded_agents == ("coder",)
    assert code.embedded_phases == ("coding",)
    assert code.danger
    assert code.collisions == ("claude-code:acme/skills:Code",)
    assert collision.canonical_identity == "acme/skills:Code"
    assert collision.manager_path == "/manager/Code"
    assert collision.danger
    assert collision.collisions == ("t3:code",)
    assert collision.removable
    assert required.required
    assert not required.removable
    assert optional.excluded
    assert optional.removable
    assert dashboard.collision_count == 1
    assert [(row.name, row.sources) for row in dashboard.required_external] == [
        ("review", ("runtime:review_skill",)),
        ("writing-plans", ("apm.yml",)),
    ]

    collision_only = filter_skill_dashboard(dashboard, query="", status="collision")
    optional_only = filter_skill_dashboard(dashboard, query="optional", status="")
    required_only = filter_skill_dashboard(dashboard, query="", status="required")
    update_only = filter_skill_dashboard(dashboard, query="", status="update")
    assert [row.identity for row in collision_only.teatree_skills] == ["t3:code"]
    assert [row.name for row in collision_only.harnesses[0].skills] == ["Code"]
    assert collision_only.harnesses[1].skills == ()
    assert optional_only.teatree_skills == ()
    assert [row.name for row in optional_only.harnesses[1].skills] == ["optional"]
    assert optional_only.collision_count == 0
    assert [row.name for row in required_only.harnesses[1].skills] == ["writing-plans"]
    assert [row.name for row in update_only.harnesses[1].skills] == ["optional"]
    assert update_only.teatree_skills == ()


def test_projection_distinguishes_declared_companion_and_transitively_embedded(tmp_path: Path) -> None:
    dashboard = build_skill_dashboard(
        source=SkillDashboardSource(_inventory(tmp_path), (), (), []),
        receipt=_receipt(),
        receipt_error=None,
        now=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    )

    code, test = dashboard.teatree_skills
    assert code.direct_agents == ("coder",)
    assert code.companion_agents == ()
    assert test.direct_agents == ()
    assert test.companion_agents == ("coder",)
    assert test.embedded_agents == ("coder",)
    assert test.embedded_phases == ("coding",)


def test_cached_receipt_missing_or_malformed_is_actionable_and_never_raises(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"

    missing = load_cached_receipt(path)
    path.write_text(json.dumps({"schemaVersion": 999}))
    malformed = load_cached_receipt(path)

    assert missing.receipt is None
    assert missing.stale
    assert "Refresh inventory" in missing.error
    assert malformed.receipt is None
    assert malformed.stale
    assert "invalid" in malformed.error.lower()


def test_projection_marks_old_receipt_stale(tmp_path: Path) -> None:
    receipt = _receipt()
    dashboard = build_skill_dashboard(
        source=SkillDashboardSource(_inventory(tmp_path), (), (), []),
        receipt=receipt,
        receipt_error=None,
        now=receipt.generated_at + timedelta(hours=25),
    )

    assert dashboard.receipt.stale
    assert dashboard.receipt.generated_at == receipt.generated_at


def test_projection_without_receipt_includes_unresolved_requirements(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    inventory = SkillInventory(
        inventory.declarations,
        (
            SkillInventoryDiagnostic(
                SkillInventoryDiagnosticCode.MISSING_AGENT_COMPANION,
                "agent:coder",
                "external:review-helper",
            ),
        ),
        inventory.cycles,
        inventory.agent_closures,
        inventory.phase_closures,
    )

    dashboard = build_skill_dashboard(
        source=SkillDashboardSource(inventory, (), (), []),
        receipt=None,
        receipt_error=None,
        now=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    )

    assert dashboard.receipt.stale
    assert "Refresh inventory" in dashboard.receipt.error
    assert dashboard.harnesses[0].skills == ()
    assert dashboard.required_external[0].name == "review-helper"
    assert dashboard.diagnostics == ("missing_agent_companion: agent:coder -> external:review-helper",)
