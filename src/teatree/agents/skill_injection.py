"""Render loaded skills into prompt text.

A headless or sub-agent dispatch inherits none of the orchestrator's loaded
skills, so the dispatched prompt must carry required skill text. This module
resolves skill names for the agent system context (``prompt.build_system_context``)
and the raw Agent-tool sub-agent preamble (``build_subagent_skill_preamble``).
It depends only on the filesystem, never the ORM, so a Django-free caller (the
``t3 <overlay> skill-preamble`` CLI) can emit a preamble without bootstrapping Django.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from teatree.agents.skill_assurance import SkillDispatchError, required_load_line
from teatree.agents.skill_files import reach_line, rules_document_path
from teatree.skill_support.index import bare_skill_name, harness_skills_dirs, resolve_skill_md, skill_tool_load_name
from teatree.skill_support.loading import FRAMEWORK_SKILL_NAMES

_ALWAYS_FULL_SKILLS = frozenset({"rules"})
_READ_ONLY_SKILL_SECTIONS = {
    "sweeping-tickets": frozenset(
        {
            "## Non-Negotiables",
            "## Command Reference",
            "## Workflow",
            "## Scheduling via the loop",
            "## Dream gaps ride the same sweep",
            "## Rules",
            "## Related skills",
        }
    ),
    "review": frozenset(
        {
            "### Quality Gate Verification (Verify-Fix-Repeat)",
            "### Receiving Code Review",
            "### Recording a GIVE-review approval — `t3 review approve` (Mandatory)",
            "### Concluding a no-postable-action external review — `mark_review_no_action` (Mandatory, #1077)",
            "## Commands",
        }
    ),
    "ticket": frozenset({"### 2a. The Acceptance Criteria Live on the Plan"}),
    "slack-formatting": frozenset(
        {
            "## Tables — use the formatter, never a hand-rolled pipe table",
            "## mrkdwn gotchas",
            "## Line length — wrapped at 90, at the transport",
            "## CLI tables",
        }
    ),
}
_HEADING = re.compile(r"^(#{2,6}) ")


def _read_only_skill_text(name: str, content: str, *, sweep_skill_name: str = "sweeping-tickets") -> str:
    sections_key = "sweeping-tickets" if name == sweep_skill_name else name
    omitted = _READ_ONLY_SKILL_SECTIONS.get(sections_key, frozenset())
    if not omitted:
        return content
    kept: list[str] = []
    skipped_level = 0
    for line in content.splitlines(keepends=True):
        heading = _HEADING.match(line)
        if heading and skipped_level and len(heading[1]) <= skipped_level:
            skipped_level = 0
        if heading and line.rstrip() in omitted:
            skipped_level = len(heading[1])
        if not skipped_level:
            kept.append(line)
    return "".join(kept)


def _resolve_dirs(skills_dir: Path | None) -> list[Path]:
    """The ordered search roots: an explicit single dir, else the harness dirs."""
    return [skills_dir] if skills_dir is not None else harness_skills_dirs()


def _skill_section(name: str, content: str) -> str:
    """Render one embedded SKILL.md block — the shared inline-skill format."""
    return f"--- SKILL: {name} ---\n{content}"


def _with_reach_line(sections: list[str], dirs: Sequence[Path]) -> str:
    """Join *sections* under the reach line, which leads so a budget cut can never drop it."""
    return "\n\n".join((reach_line(dirs[0]), *sections)) if sections else ""


def _is_stack_skill(name: str) -> bool:
    return _explicit_load_name(name) in FRAMEWORK_SKILL_NAMES


def _stack_load_block(names: Sequence[str], dirs: Sequence[Path]) -> list[str]:
    """The stack skills as required loads — never embedded, never a skippable companion line."""
    if not names:
        return []
    lines = (required_load_line(_explicit_load_name(name), resolve_skill_md(name, dirs)) for name in names)
    return ["--- STACK SKILLS (REQUIRED — load before you start) ---\n" + "\n".join(lines)]


def _read_skill_contents(skills: list[str], *, skills_dir: Path | None = None) -> str:
    """Embed each resolved skill's SKILL.md; the stack skills lead as required loads instead."""
    dirs = _resolve_dirs(skills_dir)
    sections = _stack_load_block([name for name in skills if _is_stack_skill(name)], dirs)
    for name in skills:
        skill_md = None if _is_stack_skill(name) else resolve_skill_md(name, dirs)
        if skill_md is not None:
            sections.append(_skill_section(bare_skill_name(name), skill_md.read_text(encoding="utf-8")))
    return _with_reach_line(sections, dirs)


def _companion_line(name: str, dirs: Sequence[Path]) -> str:
    skill_md = resolve_skill_md(name, dirs)
    if skill_md is None:
        return f"- {name}: not embedded — no SKILL.md resolves for it on this host"
    return f"- {name}: not embedded — Read `{skill_md}` when it applies"


def _is_primary(name: str, primary_skills: set[str]) -> bool:
    """Check if a skill name (or path) matches the primary set or always-full list."""
    bare = bare_skill_name(name)
    if name in primary_skills or bare in primary_skills or bare in _ALWAYS_FULL_SKILLS:
        return True
    skill_dir_name = Path(name).parent.name if "/" in name else ""
    return skill_dir_name in primary_skills or skill_dir_name in _ALWAYS_FULL_SKILLS


def _explicit_load_name(name: str) -> str:
    """Return the bare ``/skill`` reference for an explicit-load instruction."""
    return Path(name).parent.name if "/" in name else name


def _read_skill_contents_scoped(
    skills: list[str],
    *,
    primary_skills: set[str],
    explicit_load_skills: set[str] | None = None,
    suppress_names: set[str] | None = None,
    skills_dir: Path | None = None,
) -> str:
    """Read skills with scoping.

    Primary skills (the lifecycle skill, ``rules``, and — on the reviewing
    phase — the overlay's primary review skills) get full content. Skills in
    *explicit_load_skills* get a verbatim "Load /<skill> via the Skill tool
    BEFORE reviewing" instruction instead of the companion line. Skills in *suppress_names* are
    omitted entirely — the caller force-loads them elsewhere (e.g. the coding
    directive's stack-load block, #1368), so listing them in the ignorable
    summary would contradict that. A stack skill (``ac-django`` / ``ac-python`` /
    ``fastapi``) leads the block as a required load. Everything else gets a
    companion line naming the absolute ``SKILL.md`` path to ``Read`` when it
    applies — or saying no body resolves on this host, rather than advertising
    one that is not there. Both companion lists follow the stack loads and lead
    the embedded bodies, so a budget cut sheds bodies first.
    """
    dirs = _resolve_dirs(skills_dir)
    explicit = explicit_load_skills or set()
    suppress = suppress_names or set()
    sections: list[str] = []
    companion_names: list[str] = []
    explicit_names: list[str] = []
    stack_names: list[str] = []
    for name in skills:
        if _is_primary(name, primary_skills):
            skill_md = resolve_skill_md(name, dirs)
            if skill_md is not None:
                sections.append(_skill_section(bare_skill_name(name), skill_md.read_text(encoding="utf-8")))
        elif name in explicit or _explicit_load_name(name) in explicit:
            explicit_names.append(name)
        elif name in suppress or _explicit_load_name(name) in suppress:
            continue
        elif _is_stack_skill(name):
            stack_names.append(name)
        else:
            companion_names.append(name)
    companion_list: list[str] = []
    if explicit_names:
        block = "--- REVIEW COMPANION SKILLS (REQUIRED — load before reviewing) ---\n"
        block += "\n".join(
            f"Load /{skill_tool_load_name(name)} via the Skill tool BEFORE reviewing." for name in explicit_names
        )
        companion_list.append(block)
    if companion_names:
        summary = "--- COMPANION SKILLS (not embedded, to save context) ---\n"
        summary += "\n".join(_companion_line(name, dirs) for name in companion_names)
        companion_list.append(summary)
    return _with_reach_line([*_stack_load_block(stack_names, dirs), *companion_list, *sections], dirs)


def read_only_venue_skill_contents(
    skills: list[str], embedded: set[str], *, sweep_skill_name: str = "sweeping-tickets"
) -> str:
    """Embed primary skills and the rules document; this venue mounts no skill files."""
    dirs = harness_skills_dirs()
    sections: list[str] = []
    absent: list[str] = []
    for name in skills:
        bare = bare_skill_name(name)
        skill_md = resolve_skill_md(name, dirs) if _is_primary(name, embedded) else None
        if skill_md is None:
            absent.append(bare)
        else:
            if bare == "rules":
                rules_md = rules_document_path(skill_md)
                try:
                    rules_text = rules_md.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    missing = f"{bare}/{rules_md.name}"
                    raise SkillDispatchError.missing_document(bare, missing) from exc
                sections.append(f"--- RULES.md ---\n{rules_text}")
            sections.append(
                _skill_section(
                    bare,
                    _read_only_skill_text(
                        bare, skill_md.read_text(encoding="utf-8"), sweep_skill_name=sweep_skill_name
                    ),
                )
            )
    reach = "Only the skill text below reaches this venue: no skill file it cites is mounted here."
    if absent:
        reach = f"{reach} Not available here: {', '.join(absent)}."
    rules = [section for section in sections if section.startswith(("--- RULES.md ---", "--- SKILL: rules ---"))]
    remaining = [section for section in sections if section not in rules]
    return "\n\n".join((reach, *rules, *remaining))


_SUBAGENT_PREAMBLE_HEADER = (
    "# Required skills (a dispatched sub-agent does not auto-load them)\n"
    "The rules text is embedded below. Run skill-preamble only if you dispatch another agent.\n"
    "Follow the embedded rules and load each required stack skill via the Skill tool,\n"
    "before reading files, running commands, or writing code."
)


@dataclass(frozen=True)
class SkillPreamble:
    """The skill text a raw Agent-tool sub-agent brief must carry.

    ``text`` is the header, resolved skill bodies, and stack load directives (empty when no
    requested skill resolved). ``resolved`` and ``missing`` carry the bare skill
    names that were and were not found, so a caller can fail loud on a typo or
    an overlay skill absent for the active overlay rather than ship a brief that
    silently dropped a rule set.
    """

    text: str
    resolved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def build_subagent_skill_preamble(skills: list[str], *, skills_dirs: Sequence[Path]) -> SkillPreamble:
    """Render requested skills once into a sub-agent dispatch preamble.

    A sub-agent spawned through the raw harness Agent tool inherits none of the
    orchestrator's loaded skills, so the orchestrator must prepend this text.
    Stack skills are required loads; other resolved skills carry their bodies.
    """
    stack: list[str] = []
    bodies: list[str] = []
    resolved: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for name in skills:
        bare = bare_skill_name(name)
        if bare in seen:
            continue
        seen.add(bare)
        skill_md = resolve_skill_md(name, skills_dirs)
        if skill_md is None:
            missing.append(bare)
        elif _is_stack_skill(bare):
            stack.append(bare)
            resolved.append(bare)
        else:
            bodies.append(_skill_section(bare, skill_md.read_text(encoding="utf-8")))
            resolved.append(bare)
    sections = [*_stack_load_block(stack, skills_dirs), *bodies]
    text = f"{_SUBAGENT_PREAMBLE_HEADER}\n\n" + _with_reach_line(sections, skills_dirs) if sections else ""
    return SkillPreamble(text=text, resolved=resolved, missing=missing)
