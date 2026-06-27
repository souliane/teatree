"""Render loaded skills into prompt text.

A headless or sub-agent dispatch inherits none of the orchestrator's loaded
skills, so the dispatched prompt must carry the ``SKILL.md`` bodies inline. This
module resolves skill names to their ``SKILL.md`` files and concatenates the
bodies — the shared building block for both the headless system context
(``prompt.build_system_context``) and the raw Agent-tool sub-agent preamble
(``build_subagent_skill_preamble``). It depends only on the filesystem, never
the ORM, so a Django-free caller (the ``t3 <overlay> skill-preamble`` CLI) can
emit a preamble without bootstrapping Django.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from teatree.skill_support.loading import DEFAULT_SKILLS_DIR

_ALWAYS_FULL_SKILLS = frozenset({"rules"})


def _find_skill_md(name: str, skills_dir: Path | None = None) -> Path | None:
    """Locate SKILL.md for a skill name within the skills directory."""
    sd = skills_dir if skills_dir is not None else DEFAULT_SKILLS_DIR
    candidate = sd / name / "SKILL.md"
    return candidate if candidate.is_file() else None


def _skill_section(name: str, content: str) -> str:
    """Render one embedded SKILL.md block — the shared inline-skill format."""
    return f"--- SKILL: {name} ---\n{content}"


def _read_skill_contents(skills: list[str], *, skills_dir: Path | None = None) -> str:
    """Read and concatenate SKILL.md content for each resolved skill."""
    sd = skills_dir if skills_dir is not None else DEFAULT_SKILLS_DIR
    sections: list[str] = []
    for name in skills:
        skill_md = _find_skill_md(name, sd)
        if skill_md is not None:
            sections.append(_skill_section(name, skill_md.read_text(encoding="utf-8")))
    return "\n\n".join(sections)


def _is_primary(name: str, primary_skills: set[str]) -> bool:
    """Check if a skill name (or path) matches the primary set or always-full list."""
    if name in primary_skills or name in _ALWAYS_FULL_SKILLS:
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
    BEFORE reviewing" instruction instead of the generic, easy-to-ignore
    "available — load if needed" summary. Skills in *suppress_names* are
    omitted entirely — the caller force-loads them elsewhere (e.g. the coding
    directive's stack-load block, #1368), so listing them in the ignorable
    summary would contradict that. Everything else gets the generic summary.
    """
    sd = skills_dir if skills_dir is not None else DEFAULT_SKILLS_DIR
    explicit = explicit_load_skills or set()
    suppress = suppress_names or set()
    sections: list[str] = []
    companion_names: list[str] = []
    explicit_names: list[str] = []
    for name in skills:
        if _is_primary(name, primary_skills):
            skill_md = _find_skill_md(name, sd)
            if skill_md is not None:
                sections.append(_skill_section(name, skill_md.read_text(encoding="utf-8")))
        elif name in explicit or _explicit_load_name(name) in explicit:
            explicit_names.append(name)
        elif name in suppress or _explicit_load_name(name) in suppress:
            continue
        else:
            companion_names.append(name)
    if explicit_names:
        block = "--- REVIEW COMPANION SKILLS (REQUIRED — load before reviewing) ---\n"
        block += "\n".join(
            f"Load /{_explicit_load_name(name)} via the Skill tool BEFORE reviewing." for name in explicit_names
        )
        sections.append(block)
    if companion_names:
        summary = "--- COMPANION SKILLS (loaded but summarized to save context) ---\n"
        summary += "\n".join(f"- {name}: available — load if needed" for name in companion_names)
        sections.append(summary)
    return "\n\n".join(sections)


def _bare_skill_name(name: str) -> str:
    """Reduce a skill reference to its on-disk directory name.

    A reference reaches this helper qualified (``t3:rules``), as a bare name
    (``rules``), or as an explicit path (``skills/rules/SKILL.md``). Each maps
    to the ``skills/<dir>/SKILL.md`` directory that holds the body, so the
    namespace qualifier and any path prefix are dropped to the directory name —
    the filesystem key the preamble resolves against.
    """
    tail = name.rsplit(":", 1)[-1]
    if tail.endswith("/SKILL.md"):
        return Path(tail).parent.name
    return Path(tail).name


def _resolve_skill_md(name: str, skills_dirs: Sequence[Path]) -> Path | None:
    """Find the first ``<dir>/<skill>/SKILL.md`` across *skills_dirs*, in order."""
    bare = _bare_skill_name(name)
    for sd in skills_dirs:
        found = _find_skill_md(bare, sd)
        if found is not None:
            return found
    return None


_SUBAGENT_PREAMBLE_HEADER = (
    "# Required skills (a dispatched sub-agent does not auto-load them)\n"
    "Follow every rule in the skills below as if you had loaded them via the Skill tool,\n"
    "before reading files, running commands, or writing code."
)


@dataclass(frozen=True)
class SkillPreamble:
    """The inline skill bodies a raw Agent-tool sub-agent brief must carry.

    ``text`` is the concatenated header + ``SKILL.md`` bodies (empty when no
    requested skill resolved). ``resolved`` and ``missing`` carry the bare skill
    names that were and were not found, so a caller can fail loud on a typo or
    an overlay skill absent for the active overlay rather than ship a brief that
    silently dropped a rule set.
    """

    text: str
    resolved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def build_subagent_skill_preamble(skills: list[str], *, skills_dirs: Sequence[Path]) -> SkillPreamble:
    """Concatenate each skill's ``SKILL.md`` into a sub-agent dispatch preamble.

    A sub-agent spawned through the raw harness Agent tool inherits none of the
    orchestrator's loaded skills, so the orchestrator must prepend the skill
    bodies to the brief. Each name resolves against *skills_dirs* in order
    (the framework skills dir first, then the active overlay's skills dir), so
    an overlay skill body reaches the sub-agent exactly as a framework one does.
    """
    sections: list[str] = []
    resolved: list[str] = []
    missing: list[str] = []
    for name in skills:
        skill_md = _resolve_skill_md(name, skills_dirs)
        if skill_md is None:
            missing.append(name)
            continue
        bare = _bare_skill_name(name)
        sections.append(_skill_section(bare, skill_md.read_text(encoding="utf-8")))
        resolved.append(bare)
    text = f"{_SUBAGENT_PREAMBLE_HEADER}\n\n" + "\n\n".join(sections) if sections else ""
    return SkillPreamble(text=text, resolved=resolved, missing=missing)
