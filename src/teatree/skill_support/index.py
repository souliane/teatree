"""The one skill resolver and the index built from it, over every skill root.

Every reader — the headless bundle, ``t3 agent``, the SessionStart cache, the hook
closure, the prompt embed — resolves a name through :func:`resolve_skill_md`, and
:func:`build_skill_index` keys each name to the ``SKILL.md`` that same resolver
picks, so the ``requires`` closure and the embedded bodies can never disagree on
which root's skill a name means. A caller without an index gets this one, never an
empty list: an empty index drops every ``requires`` edge without a word (#4769).
"""

import operator
from collections.abc import Sequence
from pathlib import Path

from teatree.skill_support.deps import SkillIndex
from teatree.skill_support.pin_shadow import refuse_shadowed_pin
from teatree.skill_support.requires_parser import parse_companions, parse_requires

_SKILL_FILE = "SKILL.md"


def _default_skills_dir() -> Path:
    from teatree import find_project_root  # noqa: PLC0415 — deferred: call-time import, kept lazy

    root = find_project_root()
    if root:
        return root / "skills"
    # Fallback for non-source installs: skills/ next to src/
    return Path(__file__).resolve().parents[3] / "skills"


DEFAULT_SKILLS_DIR = _default_skills_dir()


def install_roots() -> list[Path]:
    """The folders an installer manages — the only place a declared pin may resolve from."""
    home = Path.home()
    return [home / ".agents" / "skills", home / ".claude" / "skills", home / ".codex" / "skills"]


def harness_skills_dirs() -> list[Path]:
    """Every skill root in resolution order: this checkout's ``skills/``, then each install root."""
    return list(dict.fromkeys([DEFAULT_SKILLS_DIR, *install_roots()]))


def bare_skill_name(name: str) -> str:
    """Reduce ``t3:rules``, ``rules`` or ``skills/rules/SKILL.md`` to the directory name ``rules``."""
    tail = name.rsplit(":", 1)[-1]
    if tail.endswith(f"/{_SKILL_FILE}"):
        return Path(tail).parent.name
    return Path(tail).name


def resolve_skill_md(name: str, skills_dirs: Sequence[Path]) -> Path | None:
    """The first ``<root>/<skill>/SKILL.md`` across *skills_dirs*, refusing a shadowed apm pin."""
    bare = bare_skill_name(name)
    installs = install_roots()
    for root in skills_dirs:
        candidate = root / bare / _SKILL_FILE
        if candidate.is_file():
            if root not in installs:
                refuse_shadowed_pin(bare, candidate)
            return candidate
    return None


def _skill_names(skills_dirs: Sequence[Path]) -> list[str]:
    names = {
        entry.name
        for root in skills_dirs
        if root.is_dir()
        for entry in root.iterdir()
        if (entry / _SKILL_FILE).is_file()
    }
    return sorted(names)


def build_skill_index(skills_dirs: Sequence[Path]) -> SkillIndex:
    """One ``{"skill", "requires", "companions"}`` entry per name, read from the winning root."""
    index: SkillIndex = []
    for name in _skill_names(skills_dirs):
        skill_md = resolve_skill_md(name, skills_dirs)
        if skill_md is None:
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        index.append(
            {"skill": name, "requires": parse_requires(text) or [], "companions": parse_companions(text) or []}
        )
    index.sort(key=operator.itemgetter("skill"))
    return index


def direct_requires(name: str, skills_dirs: Sequence[Path]) -> list[str]:
    """The ``requires:`` list of *name*'s winning ``SKILL.md``, one level deep."""
    skill_md = resolve_skill_md(name, skills_dirs)
    if skill_md is None:
        return []
    return parse_requires(skill_md.read_text(encoding="utf-8")) or []


def skill_mtimes(skills_dirs: Sequence[Path]) -> dict[str, int]:
    """``{SKILL.md path: mtime_ns}`` across every root — the cache's staleness fingerprint."""
    mtimes: dict[str, int] = {}
    for root in skills_dirs:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            skill_md = entry / _SKILL_FILE
            try:
                mtimes[str(skill_md)] = skill_md.stat().st_mtime_ns
            except OSError:
                continue
    return mtimes


__all__ = [
    "DEFAULT_SKILLS_DIR",
    "bare_skill_name",
    "build_skill_index",
    "direct_requires",
    "harness_skills_dirs",
    "install_roots",
    "resolve_skill_md",
    "skill_mtimes",
]
