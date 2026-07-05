"""Transitive dependency resolution for skill ``requires`` fields.

Builds a dependency graph from the skill index and resolves skills in
topological order — dependencies before dependents.  Cycle detection uses
DFS gray/black colouring.

``requires`` is the hard skill-dependency edge: always transitive, always
topologically ordered. A required skill with no SKILL.md (an external
framework skill such as ``test-driven-development``) passes through unchanged
so the ``Skill`` tool still loads it; the loading policy warns about it.

``companions`` is the SOFT counterpart handled by :func:`companion_suggestions`:
surfaced but never enforced, and deliberately NOT transitive — a companion is a
suggestion, not a dependency, so it is not folded into the hard requires chain.
"""

# Skill index entries are dicts with ``skill``, ``requires``, and optional ``companions``.
type SkillIndex = list[dict[str, object]]

# Gray = currently being visited (cycle if re-entered), Black = fully resolved.
_GRAY, _BLACK = 1, 2


def resolve_requires(
    skills: list[str],
    skill_index: SkillIndex,
) -> list[str]:
    """Return *skills* expanded with transitive ``requires`` in topological order.

    Unknown skills (not in *skill_index*) pass through unchanged — they may be
    framework skills (``ac-django``, ``ac-python``) or external methodology
    skills (``test-driven-development``) that have no index entry.

    Raises ``ValueError`` on dependency cycles.
    """
    requires_map = _build_requires_map(skill_index)
    order: list[str] = []
    state: dict[str, int] = {}

    for skill in skills:
        _visit(skill, requires_map, state, order)

    return order


def resolve_all(skill_index: SkillIndex) -> dict[str, list[str]]:
    """Pre-compute resolved dependencies for every skill in *skill_index*.

    Returns ``{skill_name: [dep1, dep2, ..., skill_name]}`` — each value
    is the full topologically-sorted load order including the skill itself.
    """
    requires_map = _build_requires_map(skill_index)
    result: dict[str, list[str]] = {}

    for entry in skill_index:
        skill = str(entry.get("skill", ""))
        if not skill:
            continue
        order: list[str] = []
        state: dict[str, int] = {}
        try:
            _visit(skill, requires_map, state, order)
        except ValueError:
            # Cycle — store the skill alone so it still loads.
            order = [skill]
        result[skill] = order

    return result


def companion_suggestions(resolved: list[str], skill_index: SkillIndex) -> list[str]:
    """Soft companion suggestions for an already-resolved (hard) skill set.

    Unlike ``requires`` (the hard, transitive edge in :func:`resolve_requires`),
    ``companions`` are surfaced but never enforced: this returns every skill named
    in a resolved skill's ``companions`` list, in first-seen order, excluding any
    skill already hard-resolved. It is deliberately NOT transitive — a companion's
    own ``requires``/``companions`` are not pulled in.
    """
    companion_map = _build_list_field_map(skill_index, "companions")
    already_resolved = set(resolved)
    seen: set[str] = set()
    suggestions: list[str] = []
    for skill in resolved:
        for companion in companion_map.get(skill, []):
            if companion and companion not in already_resolved and companion not in seen:
                seen.add(companion)
                suggestions.append(companion)
    return suggestions


def _build_requires_map(skill_index: SkillIndex) -> dict[str, list[str]]:
    return _build_list_field_map(skill_index, "requires")


def _build_list_field_map(skill_index: SkillIndex, field_name: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for entry in skill_index:
        skill = str(entry.get("skill", ""))
        if not skill:
            continue
        raw = entry.get(field_name, [])
        result[skill] = [str(item) for item in raw] if isinstance(raw, list) else []
    return result


def _visit(
    skill: str,
    requires_map: dict[str, list[str]],
    state: dict[str, int],
    order: list[str],
) -> None:
    if state.get(skill) == _BLACK:
        return
    if state.get(skill) == _GRAY:
        msg = f"Circular dependency detected involving '{skill}'"
        raise ValueError(msg)

    state[skill] = _GRAY

    for dep in requires_map.get(skill, []):
        _visit(dep, requires_map, state, order)

    state[skill] = _BLACK
    order.append(skill)
