"""Transitive dependency resolution for skill ``requires`` fields.

Builds a dependency graph from the trigger index and resolves skills
in topological order — dependencies before dependents.  Cycle detection
uses DFS gray/black colouring.
"""

# Gray = currently being visited (cycle if re-entered), Black = fully resolved.
_GRAY, _BLACK = 1, 2


def resolve_requires(
    skills: list[str],
    trigger_index: list[dict[str, object]],
) -> list[str]:
    """Return *skills* expanded with transitive ``requires`` in topological order.

    Unknown skills (not in *trigger_index*) pass through unchanged — they
    may be framework skills (``ac-django``, ``ac-python``) that have no
    trigger entry.

    Raises ``ValueError`` on dependency cycles.
    """
    requires_map = _build_requires_map(trigger_index)
    order: list[str] = []
    state: dict[str, int] = {}

    for skill in skills:
        _visit(skill, requires_map, state, order)

    return order


def resolve_all(trigger_index: list[dict[str, object]]) -> dict[str, list[str]]:
    """Pre-compute resolved dependencies for every skill in *trigger_index*.

    Returns ``{skill_name: [dep1, dep2, ..., skill_name]}`` — each value
    is the full topologically-sorted load order including the skill itself.
    """
    requires_map = _build_requires_map(trigger_index)
    result: dict[str, list[str]] = {}

    for entry in trigger_index:
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


def _build_requires_map(trigger_index: list[dict[str, object]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for entry in trigger_index:
        skill = str(entry.get("skill", ""))
        if not skill:
            continue
        raw = entry.get("requires", [])
        requires = [str(r) for r in raw] if isinstance(raw, list) else []
        result[skill] = requires
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
