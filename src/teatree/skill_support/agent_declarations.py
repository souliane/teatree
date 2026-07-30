"""The ``agents/*.md`` frontmatter ``skills:`` declaration — one source, both lanes (#3667).

Interactive dispatch resolves a sub-agent's skills from its agent file's
frontmatter. Headless dispatch used a parallel hard-coded phase→skill table, so a
phase got exactly ONE skill: ``agents/coder.md`` declares four
(``rules``/``workspace``/``architecture-design``/``code``) and headless coding
loaded only ``code``, silently dropping the architecture pass. This module is the
declaration reader both lanes now share, so the two cannot disagree.

Pure and platform-layer by design: it parses a markdown frontmatter block and
knows nothing about phases, roles, or the dispatch map. The phase→agent
resolution that consumes it lives in :mod:`teatree.agents.phase_agent_skills`,
where the domain-layer dispatch table is importable.
"""

from pathlib import Path

#: Frontmatter key whose list items are the agent's declared skills.
_SKILLS_KEY = "skills"
_FENCE = "---"


def default_agents_dir() -> Path:
    """The shipped ``agents/`` directory next to ``src/``."""
    return Path(__file__).resolve().parents[3] / "agents"


def agent_declared_skills(agent_path: Path) -> list[str]:
    """The ordered ``skills:`` list declared in *agent_path*'s frontmatter.

    An absent file, an unreadable one, or one with no leading ``---`` fence
    declares nothing — a phase with no agent file falls back to the legacy
    phase→skill map rather than losing its skill entirely.
    """
    try:
        text = agent_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not text.startswith(_FENCE):
        return []
    skills: list[str] = []
    in_skills = False
    for number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if number > 1 and stripped == _FENCE:
            break
        if not raw_line.startswith((" ", "\t")) and ":" in stripped:
            in_skills = stripped.split(":", 1)[0].strip() == _SKILLS_KEY
            continue
        if in_skills and stripped.startswith("- "):
            name = stripped.removeprefix("- ").strip().strip("'\"")
            if name:
                skills.append(name)
    return skills


def declared_skills_for_agent(agent: str, *, agents_dir: Path | None = None) -> list[str]:
    """The skills ``agents/<agent>.md`` declares, or ``[]`` when there is no such file."""
    directory = agents_dir if agents_dir is not None else default_agents_dir()
    return agent_declared_skills(directory / f"{agent}.md")


__all__ = ["agent_declared_skills", "declared_skills_for_agent", "default_agents_dir"]
