"""Bounded, truthful receipts for skills requested by a headless dispatch."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from teatree.agents.skill_injection import _bare_skill_name, _resolve_skill_md, harness_skills_dirs

_EVIDENCE = re.compile(r"^[A-Za-z0-9_./:# -]{4,160}$")
_LOAD_DIRECTIVE = re.compile(
    r"^[ \t]*(?:REQUIRED:[ \t]*)?Load[ \t]+/((?:t3:)?[A-Za-z0-9_.-]+)"
    r"[ \t]+via[ \t]+the[ \t]+Skill[ \t]+tool\b",
    re.IGNORECASE,
)
_STACK_LOAD_BULLET = re.compile(r"^[ \t]*-[ \t]+/((?:t3:)?[A-Za-z0-9_.-]+)[ \t]*$")
_CODING_LOAD_LINE = "REQUIRED: before writing code, call the Skill tool for /t3:architecture-design and /t3:code."
_STACK_LOAD_HEADER = "REQUIRED: before writing code, also call the Skill tool for EACH of these stack/overlay skills:"


class SkillEvidence(TypedDict):
    skill: str
    evidence: str


class SkillAssurance(TypedDict):
    requested: list[str]
    found: list[str]
    injected: list[str]
    explicit_load: list[str]
    missing: list[str]
    evidence: list[SkillEvidence]
    observed_loads: list[str]
    status: str


class SkillDispatchError(ValueError):
    """A mandatory skill was absent or not delivered before a billable turn."""

    def __init__(self, assurance: SkillAssurance) -> None:
        self.assurance = assurance
        detail = ", ".join(assurance["missing"] or sorted(set(assurance["requested"]) - set(assurance["injected"])))
        super().__init__(f"skill {assurance['status']}: {detail}")


def _required_names(skills: Sequence[str], required: set[str]) -> list[str]:
    names = [_bare_skill_name(name) for name in skills if name in required or _bare_skill_name(name) in required]
    for name in sorted(required):
        bare = _bare_skill_name(name)
        if bare not in names:
            names.append(bare)
    return list(dict.fromkeys(names))


def _explicit_directive_names(rendered_context: str) -> set[str]:
    """Recognize only generated load instructions, not incidental skill mentions."""
    names: set[str] = set()
    stack_block = False
    for line in rendered_context.splitlines():
        if line == _CODING_LOAD_LINE:
            names.update({"architecture-design", "code"})
        if line == _STACK_LOAD_HEADER:
            stack_block = True
            continue
        if stack_block:
            bullet = _STACK_LOAD_BULLET.fullmatch(line)
            if bullet is not None:
                names.add(_bare_skill_name(bullet.group(1)))
                continue
            stack_block = False
        if directive := _LOAD_DIRECTIVE.match(line):
            names.add(_bare_skill_name(directive.group(1)))
    return names


def recover_truncated_inline_skills(
    *,
    required_inline: set[str],
    required_explicit: set[str],
    rendered_context: str,
    skills_dirs: Sequence[Path] | None = None,
    can_load: bool,
) -> tuple[set[str], set[str], str]:
    """Turn budget-truncated bodies into explicit full-file loads when tools permit.

    A skill header surviving the 96 KiB context budget is not the whole skill.
    The agent gets both the Skill-tool reference and the exact local file path;
    its later application receipt still does not count as independent proof.
    """
    inline = set(required_inline)
    explicit = set(required_explicit)
    if not can_load:
        return inline, explicit, ""
    directories = list(skills_dirs) if skills_dirs is not None else harness_skills_dirs()
    lines = []
    for name in sorted(required_inline):
        bare = _bare_skill_name(name)
        path = _resolve_skill_md(bare, directories)
        if path is None:
            continue  # The ordinary dispatch assessment names the missing body.
        section = f"--- SKILL: {bare} ---\n{path.read_text(encoding='utf-8')}"
        if section in rendered_context:
            continue
        inline.discard(name)
        explicit.add(bare)
        lines.append(f"REQUIRED: Load /{bare} via the Skill tool before work; if unavailable, read {path} in full.")
    return inline, explicit, "\n".join(lines)


def assess_skill_dispatch(
    *,
    skills: Sequence[str],
    required_inline: set[str],
    required_explicit: set[str],
    rendered_context: str,
    skills_dirs: Sequence[Path] | None = None,
) -> SkillAssurance:
    """Refuse missing bodies/directives without treating optional companions as mandatory."""
    inline = _required_names(skills, required_inline)
    explicit = _required_names(skills, required_explicit)
    requested = list(dict.fromkeys([*inline, *explicit]))
    directories = list(skills_dirs) if skills_dirs is not None else harness_skills_dirs()
    found = [name for name in requested if _resolve_skill_md(name, directories) is not None]
    missing = [name for name in requested if name not in found]
    injected = [
        name
        for name in inline
        if (path := _resolve_skill_md(name, directories)) is not None
        and f"--- SKILL: {name} ---\n{path.read_text(encoding='utf-8')}" in rendered_context
    ]
    directives = _explicit_directive_names(rendered_context)
    explicit_load = [name for name in explicit if name in directives]
    delivery_gap = len(injected) != len(inline) or len(explicit_load) != len(explicit)
    status = "missing" if missing else "injection_gap" if delivery_gap else "unverified"
    assurance: SkillAssurance = {
        "requested": requested,
        "found": found,
        "injected": injected,
        "explicit_load": explicit_load,
        "missing": missing,
        "evidence": [],
        "observed_loads": [],
        "status": status,
    }
    if status != "unverified":
        raise SkillDispatchError(assurance)
    return assurance


def assess_skill_application(
    dispatch: SkillAssurance,
    result: Mapping[str, object],
    *,
    observed_loads: Sequence[str] = (),
) -> SkillAssurance:
    """Keep a self-report a declaration; absent, malformed, or unrelated evidence stays unverified."""
    requested = set(dispatch["requested"])
    raw = result.get("skill_application")
    evidence: list[SkillEvidence] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("skill")
            reference = item.get("evidence")
            if not isinstance(name, str) or not isinstance(reference, str):
                continue
            bare = _bare_skill_name(name)
            reference = reference.strip()
            if bare in requested and _EVIDENCE.fullmatch(reference) and not reference.startswith("/"):
                # The agent's raw text may contain a path or secret-like token.
                # Keep only its presence; the receipt is a declaration, not proof.
                evidence.append({"skill": bare, "evidence": "provided"})
    evidence = list({item["skill"]: item for item in evidence}.values())
    observed = [_bare_skill_name(name) for name in observed_loads if _bare_skill_name(name) in requested]
    explicit_observed = set(dispatch["explicit_load"]) <= set(observed)
    declared = bool(requested) and requested <= {item["skill"] for item in evidence} and explicit_observed
    return {
        **dispatch,
        "evidence": evidence,
        "observed_loads": list(dict.fromkeys(observed)),
        "status": "declared" if declared else "unverified",
    }
