from pathlib import Path

import pytest

from teatree.skill_support.inventory import (
    DependencyCycle,
    HarnessSkillInstallation,
    SkillIdentity,
    SkillInventory,
    SkillInventoryDiagnostic,
    SkillInventoryDiagnosticCode,
    SkillInventoryLoadError,
    SkillNameCollision,
)


def _write_skill(root: Path, directory: str, name: str, requires: tuple[str, ...] = ()) -> Path:
    skill_md = root / directory / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    requires_yaml = "".join(f"  - {requirement}\n" for requirement in requires)
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {name}\nrequires:\n{requires_yaml}---\n",
        encoding="utf-8",
    )
    return skill_md


def _write_agent(
    root: Path,
    name: str,
    skills: tuple[str, ...],
    companion_skills: tuple[str, ...] = (),
) -> Path:
    skill_yaml = "".join(f"  - {skill}\n" for skill in skills)
    companion_yaml = "".join(f"  - {skill}\n" for skill in companion_skills)
    agent_path = root / f"{name}.md"
    agent_path.write_text(
        f"---\nname: {name}\nskills:\n{skill_yaml}companion_skills:\n{companion_yaml}---\n",
        encoding="utf-8",
    )
    return agent_path


def test_loads_canonical_graph_and_embedded_contexts_from_real_declarations(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    foundation_path = _write_skill(skills_dir, "foundation-directory", "foundation")
    _write_skill(skills_dir, "workflow-directory", "workflow", ("t3:foundation",))
    _write_skill(skills_dir, "delivery-directory", "delivery", ("foundation", "workflow"))
    _write_agent(agents_dir, "coder", ("delivery",))

    inventory = SkillInventory.load(
        namespace="t3",
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        phase_agents={"coding": "coder"},
    )

    foundation = SkillIdentity(namespace="t3", manifest_name="foundation")
    workflow = SkillIdentity(namespace="t3", manifest_name="workflow")
    delivery = SkillIdentity(namespace="t3", manifest_name="delivery")
    declarations = {declaration.identity: declaration for declaration in inventory.declarations}
    agents = {closure.context: closure for closure in inventory.agent_closures}
    phases = {closure.context: closure for closure in inventory.phase_closures}

    assert tuple(declarations) == (delivery, foundation, workflow)
    assert foundation.qualified_name == "t3:foundation"
    assert declarations[foundation].source_path == foundation_path
    assert declarations[workflow].declared_requires == ("t3:foundation",)
    assert declarations[workflow].direct_requires == (foundation,)
    assert declarations[delivery].direct_requires == (foundation, workflow)
    assert declarations[delivery].transitive_requires == (foundation, workflow)
    assert agents["coder"].declared == (delivery,)
    assert agents["coder"].embedded == (foundation, workflow, delivery)
    assert phases["coding"].embedded == agents["coder"].embedded


def test_rejects_invalid_yaml_even_when_name_line_looks_valid(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    skill_md = skills_dir / "grouping" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(
        "---\nname: grouping\ndescription: Evidence gate: preserve this real TeaTree shape\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillInventoryLoadError) as error:
        SkillInventory.load(
            namespace="t3",
            skills_dir=skills_dir,
            agents_dir=agents_dir,
            phase_agents={},
        )

    assert error.value.diagnostics[0].code is SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER


@pytest.mark.parametrize(
    "frontmatter",
    [
        "- name\n- not-a-mapping",
        "name: review\nrequires: external",
        "name: review\nrequires:\n  - ''",
    ],
)
def test_rejects_non_mapping_or_non_string_dependency_frontmatter(tmp_path: Path, frontmatter: str) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    skill_md = skills_dir / "broken" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")

    with pytest.raises(SkillInventoryLoadError) as error:
        SkillInventory.load(namespace="t3", skills_dir=skills_dir, agents_dir=agents_dir, phase_agents={})

    assert error.value.diagnostics[0].code is SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER


def test_unreadable_manifest_has_a_typed_non_secret_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    skill_md = _write_skill(skills_dir, "broken", "broken")
    original_read_text = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path == skill_md:
            message = "credential-material"
            raise OSError(message)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(SkillInventoryLoadError) as error:
        SkillInventory.load(namespace="t3", skills_dir=skills_dir, agents_dir=agents_dir, phase_agents={})

    assert error.value.diagnostics[0].code is SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER
    assert "credential-material" not in str(error.value)


def test_reports_missing_requires_reference(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_skill(skills_dir, "workflow", "workflow", ("external-method",))

    inventory = SkillInventory.load(
        namespace="t3",
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        phase_agents={},
    )

    assert inventory.missing_references == (
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.MISSING_DEPENDENCY,
            subject="t3:workflow",
            reference="external-method",
            source_paths=(skills_dir / "workflow" / "SKILL.md",),
        ),
    )


def test_embeds_companion_dependencies_and_reports_missing_agent_references(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_skill(skills_dir, "foundation", "foundation")
    _write_skill(skills_dir, "primary", "primary")
    _write_skill(skills_dir, "helper", "helper", ("foundation",))
    agent_path = _write_agent(
        agents_dir,
        "coder",
        ("primary", "missing-required"),
        ("helper", "missing-companion"),
    )

    inventory = SkillInventory.load(
        namespace="t3",
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        phase_agents={"coding": "coder"},
    )

    foundation = SkillIdentity(namespace="t3", manifest_name="foundation")
    primary = SkillIdentity(namespace="t3", manifest_name="primary")
    helper = SkillIdentity(namespace="t3", manifest_name="helper")
    closure = inventory.agent_closures[0]
    expected_missing = (
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.MISSING_AGENT_SKILL,
            subject="agent:coder",
            reference="missing-required",
            source_paths=(agent_path,),
        ),
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.MISSING_AGENT_COMPANION,
            subject="agent:coder",
            reference="missing-companion",
            source_paths=(agent_path,),
        ),
    )

    assert closure.declared == (primary,)
    assert closure.companion_skills == (helper,)
    assert closure.embedded == (primary, foundation, helper)
    assert closure.missing_references == expected_missing
    assert inventory.missing_references == expected_missing
    phase_closure = inventory.phase_closures[0]
    assert phase_closure.companion_skills == closure.companion_skills
    assert phase_closure.embedded == closure.embedded
    assert phase_closure.missing_references == closure.missing_references


def test_reports_dependency_cycle_without_losing_graph_edges(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_skill(skills_dir, "01-beta", "beta", ("alpha",))
    _write_skill(skills_dir, "02-alpha", "alpha", ("beta",))

    inventory = SkillInventory.load(
        namespace="t3",
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        phase_agents={},
    )

    alpha = SkillIdentity(namespace="t3", manifest_name="alpha")
    beta = SkillIdentity(namespace="t3", manifest_name="beta")
    declarations = {declaration.identity: declaration for declaration in inventory.declarations}

    assert declarations[alpha].direct_requires == (beta,)
    assert declarations[alpha].transitive_requires == (beta,)
    assert inventory.cycles == (DependencyCycle(identities=(alpha, beta)),)


def test_duplicate_canonical_names_fail_with_deterministic_diagnostic(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    first_path = _write_skill(skills_dir, "01-review", "review")
    second_path = _write_skill(skills_dir, "02-review", "review")

    with pytest.raises(SkillInventoryLoadError) as error:
        SkillInventory.load(
            namespace="t3",
            skills_dir=skills_dir,
            agents_dir=agents_dir,
            phase_agents={},
        )

    assert error.value.diagnostics == (
        SkillInventoryDiagnostic(
            code=SkillInventoryDiagnosticCode.DUPLICATE_CANONICAL_IDENTITY,
            subject="t3:review",
            source_paths=(first_path, second_path),
        ),
    )
    assert str(error.value) == "skill inventory load failed: duplicate_canonical_identity t3:review"


@pytest.mark.parametrize(
    ("contents", "code"),
    [
        ("", SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER),
        ("name: hidden\ncredential-material\n", SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER),
        ("---\nname: hidden\ncredential-material\n", SkillInventoryDiagnosticCode.MALFORMED_FRONTMATTER),
        (
            "---\ndescription: credential-material\n---\n",
            SkillInventoryDiagnosticCode.MISSING_MANIFEST_NAME,
        ),
        (
            '---\nname: ""\ndescription: credential-material\n---\n',
            SkillInventoryDiagnosticCode.MISSING_MANIFEST_NAME,
        ),
    ],
)
def test_malformed_manifest_fails_with_non_secret_diagnostic(
    tmp_path: Path,
    contents: str,
    code: SkillInventoryDiagnosticCode,
) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    skill_path = skills_dir / "broken" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(contents, encoding="utf-8")

    with pytest.raises(SkillInventoryLoadError) as error:
        SkillInventory.load(
            namespace="t3",
            skills_dir=skills_dir,
            agents_dir=agents_dir,
            phase_agents={},
        )

    assert error.value.diagnostics == (
        SkillInventoryDiagnostic(code=code, subject=str(skill_path), source_paths=(skill_path,)),
    )
    assert "credential-material" not in str(error.value)


def test_flags_normalized_manifest_name_collision_without_merging_identities(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    teatree_path = _write_skill(skills_dir, "review", "review")
    harness_path = _write_skill(tmp_path / "harness-skills", "review", "Review")
    inventory = SkillInventory.load(
        namespace="t3",
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        phase_agents={},
    )
    installed = HarnessSkillInstallation(
        harness="claude",
        identity=SkillIdentity(namespace="vendor", manifest_name="Review"),
        source_path=harness_path,
    )
    unrelated = HarnessSkillInstallation(
        harness="codex",
        identity=SkillIdentity(namespace="vendor", manifest_name="testing"),
        source_path=tmp_path / "testing" / "SKILL.md",
    )
    teatree = inventory.declarations[0]

    assert inventory.same_name_hazards((installed, unrelated)) == (
        SkillNameCollision(
            normalized_manifest_name="review",
            teatree_skill=teatree,
            harness_skill=installed,
        ),
    )
    assert teatree.identity == SkillIdentity(namespace="t3", manifest_name="review")
    assert teatree.identity != installed.identity
    assert teatree.source_path == teatree_path
    assert installed.source_path == harness_path
