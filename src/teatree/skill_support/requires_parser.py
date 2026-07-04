"""Parse the ``requires:`` list from SKILL.md YAML frontmatter.

The teatree-side twin of ``scripts/lib/requires_parser.py`` — the cold hook
needs a no-teatree-import copy, teatree code imports this one. ``requires`` is
the single skill-dependency edge; there is no free-text trigger frontmatter.
"""


def _extract_frontmatter(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    try:
        end = text.index("---", 3)
    except ValueError:
        return None
    return text[3:end]


def parse_requires(skill_md_text: str) -> list[str] | None:
    """Return the skill's ``requires:`` list, or ``None`` when the field is absent.

    An empty ``requires:`` block returns ``[]`` (the field is present but has no
    members); a skill with no ``requires:`` key at all returns ``None``.
    """
    frontmatter = _extract_frontmatter(skill_md_text)
    if frontmatter is None:
        return None

    in_requires = False
    found = False
    requires: list[str] = []
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if not line.startswith((" ", "\t")) and ":" in stripped:
            in_requires = stripped.split(":", 1)[0].strip() == "requires"
            found = found or in_requires
            continue
        if in_requires and stripped.startswith("- "):
            requires.append(stripped.removeprefix("- ").strip().strip("'\""))

    return requires if found else None
