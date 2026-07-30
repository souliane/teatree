"""Parse the ``requires:`` and ``companions:`` lists from SKILL.md frontmatter.

Standalone module with no Django or teatree imports — safe to use from both
the UserPromptSubmit hook (``skill_loader.py``) and any cold hook context.
``requires`` is the hard, transitive skill-dependency edge; ``companions`` is
its SOFT counterpart — a suggested-not-mandatory list, surfaced but never
enforced. There is no free-text trigger frontmatter to parse.
"""

from __future__ import annotations  # noqa: TID251 — standalone script, no teatree package imports


def _extract_frontmatter(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    try:
        end = text.index("---", 3)
    except ValueError:
        return None
    return text[3:end]


def _parse_list_field(skill_md_text: str, field_name: str) -> list[str] | None:
    """Return the top-level YAML list under *field_name*, or ``None`` when absent.

    An empty block returns ``[]`` (the field is present but has no members); a
    missing key returns ``None``.
    """
    frontmatter = _extract_frontmatter(skill_md_text)
    if frontmatter is None:
        return None

    in_field = False
    found = False
    values: list[str] = []
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if not line.startswith((" ", "\t")) and ":" in stripped:
            in_field = stripped.split(":", 1)[0].strip() == field_name
            found = found or in_field
            continue
        if in_field and stripped.startswith("- "):
            values.append(stripped.removeprefix("- ").strip().strip("'\""))

    return values if found else None


def parse_requires(skill_md_text: str) -> list[str] | None:
    """Return the skill's ``requires:`` list, or ``None`` when the field is absent.

    An empty ``requires:`` block returns ``[]`` (the field is present but has no
    members); a skill with no ``requires:`` key at all returns ``None``.
    """
    return _parse_list_field(skill_md_text, "requires")


def parse_companions(skill_md_text: str) -> list[str] | None:
    """Return the skill's ``companions:`` list, or ``None`` when the field is absent.

    The SOFT counterpart to :func:`parse_requires`: a suggested-not-mandatory list
    of complementary skills, surfaced but not enforced at load time. Same
    absent/empty semantics — ``None`` when the key is missing, ``[]`` when present
    but empty.
    """
    return _parse_list_field(skill_md_text, "companions")
