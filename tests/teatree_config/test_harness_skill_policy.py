# test-path: cross-cutting — harness policy composes config, inventory, and removal authorization.
from dataclasses import dataclass
from pathlib import Path

import pytest

from teatree.harness_skills import (
    HarnessSkillPolicyError,
    HarnessSkillRequiredError,
    HarnessSkillTarget,
    SkillsHarness,
    authorize_harness_skill_removal,
    parse_harness_skill_exclusions,
    remove_harness_skill_exclusion,
    required_harness_skill_names,
)
from teatree.skill_support.inventory import (
    SkillDeclaration,
    SkillIdentity,
    SkillInventory,
    SkillInventoryDiagnostic,
    SkillInventoryDiagnosticCode,
)


@dataclass(frozen=True)
class NamedDependency:
    name: str


def _inventory(
    *,
    diagnostics: tuple[SkillInventoryDiagnostic, ...] = (),
    declarations: tuple[SkillDeclaration, ...] = (),
) -> SkillInventory:
    return SkillInventory(
        declarations=declarations,
        missing_references=diagnostics,
        cycles=(),
        agent_closures=(),
        phase_closures=(),
    )


def test_exclusion_parser_normalizes_deduplicates_and_sorts() -> None:
    assert parse_harness_skill_exclusions([" CODEX:Review ", "claude-code:test", "codex:review"]) == [
        "claude-code:test",
        "codex:review",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "codex:review",
        [""],
        ["review"],
        ["cursor:review"],
        ["codex:"],
        ["codex:*"],
        ["codex:--all"],
        ["codex:."],
        ["codex:.."],
        ["codex:../review"],
        ["codex:foo/bar"],
        [r"codex:foo\bar"],
        [7],
    ],
)
def test_exclusion_parser_rejects_invalid_values(raw: object) -> None:
    with pytest.raises(HarnessSkillPolicyError):
        parse_harness_skill_exclusions(raw)


def test_required_names_combine_inventory_apm_and_runtime_demands() -> None:
    diagnostics = (
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.MISSING_DEPENDENCY,
            subject="t3:code",
            reference="vendor:External",
        ),
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.MISSING_AGENT_SKILL,
            subject="coder",
            reference="Review-Bot",
        ),
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.MISSING_AGENT_COMPANION,
            subject="coder",
            reference="vendor:review-bot",
        ),
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.DUPLICATE_CANONICAL_IDENTITY,
            subject="t3:duplicate",
            reference="ignored",
        ),
    )

    required = required_harness_skill_names(
        _inventory(diagnostics=diagnostics),
        (NamedDependency("apm-one"), NamedDependency("External")),
        ("runtime-one", "vendor:review-bot"),
    )

    assert required == ("apm-one", "external", "review-bot", "runtime-one")


def test_required_skill_is_refused_before_removal() -> None:
    with pytest.raises(HarnessSkillRequiredError) as error:
        authorize_harness_skill_removal(
            SkillsHarness.CODEX,
            "review",
            inventory=_inventory(),
            apm_dependencies=(NamedDependency("review"),),
            runtime_demands=(),
        )

    assert error.value.target == HarnessSkillTarget(SkillsHarness.CODEX, "review")
    assert error.value.required_by == ("apm.yml",)


def test_same_name_teatree_skill_is_a_removable_harness_duplicate() -> None:
    declaration = SkillDeclaration(
        identity=SkillIdentity(namespace="t3", manifest_name="review"),
        source_path=Path("/teatree/skills/review/SKILL.md"),
        declared_requires=(),
        direct_requires=(),
        transitive_requires=(),
    )

    target = authorize_harness_skill_removal(
        SkillsHarness.CLAUDE_CODE,
        "Review",
        inventory=_inventory(declarations=(declaration,)),
        apm_dependencies=(),
        runtime_demands=(),
    )

    assert target == HarnessSkillTarget(SkillsHarness.CLAUDE_CODE, "review")


def test_successful_install_helper_removes_only_the_selected_exclusion() -> None:
    assert remove_harness_skill_exclusion(
        ["codex:review", "claude-code:review", "codex:test"],
        SkillsHarness.CODEX,
        " Review ",
    ) == ["claude-code:review", "codex:test"]
