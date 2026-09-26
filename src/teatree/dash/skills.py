"""How the dashboard reads an attempt's recorded skill bundle (#3886).

One rule, shared by every surface that lists a task — the ticket drawer, the session
index and the live-work view — so "no skills" cannot read as a fault on one page and
as blank on another.

The bundle is the single biggest determinant of how an agent behaves in a phase, and
it is assembled from several independent sources that can each silently contribute
nothing: the phase's ``agents/<name>.md`` frontmatter, cwd-driven detection, the
transitive ``requires:`` chain resolved against a cached index, and the active
overlay's companion/stage skills. A cold cache, an overlay that resolves to nothing,
or a detector that does not fire all degrade the bundle quietly — the dispatch still
runs and still looks entirely normal. Rendering the empty result as blank is what
makes that invisible, so an empty bundle is reported as a FAULT instead.

Read from what the dispatch RECORDED (``TaskAttempt.skills_loaded``), never re-derived
at render time: a re-derivation reports today's answer for yesterday's dispatch, which
is precisely the bug class this surface exists to expose.
"""

import re
from dataclasses import dataclass

from teatree.core.models.task_attempt import TaskAttempt

_SAFE_SKILL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9:_./-]{0,79}$")
_SKILL_COUNT = 20
_RECENT_ATTEMPTS = 100


@dataclass(frozen=True, slots=True)
class SkillAssurance:
    requested: tuple[str, ...]
    found: tuple[str, ...]
    injected: tuple[str, ...]
    explicit_load: tuple[str, ...]
    observed_loads: tuple[str, ...]
    missing: tuple[str, ...]
    evidence_skills: tuple[str, ...]
    status: str
    label: str


def _safe_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(name for name in value[:_SKILL_COUNT] if isinstance(name, str) and _SAFE_SKILL.fullmatch(name))


def skill_assurance(value: object) -> SkillAssurance | None:
    """Project a persisted receipt, never an agent's free-text evidence body."""
    if not isinstance(value, dict):
        return None
    receipt = value.get("skill_assurance")
    if not isinstance(receipt, dict):
        return None
    requested = _safe_names(receipt.get("requested"))
    evidence = receipt.get("evidence")
    evidence_skills = tuple(
        name
        for item in (evidence[:_SKILL_COUNT] if isinstance(evidence, list) else ())
        if isinstance(item, dict)
        and isinstance(item.get("evidence"), str)
        and item["evidence"]
        and (name := item.get("skill")) in requested
    )
    status = receipt.get("status")
    if status not in {"missing", "injection_gap", "unverified", "declared"}:
        status = "unverified"
    if status == "declared" and (not requested or not set(requested).issubset(evidence_skills)):
        status = "unverified"
    label = {
        "missing": "missing skill",
        "injection_gap": "injection gap",
        "unverified": "unverified application",
        "declared": "agent-declared application",
    }[status]
    return SkillAssurance(
        requested=requested,
        found=_safe_names(receipt.get("found")),
        injected=_safe_names(receipt.get("injected")),
        explicit_load=_safe_names(receipt.get("explicit_load")),
        observed_loads=_safe_names(receipt.get("observed_loads")),
        missing=_safe_names(receipt.get("missing")),
        evidence_skills=evidence_skills,
        status=status,
        label=label,
    )


def recent_skill_assurance() -> tuple[tuple[int, SkillAssurance], ...]:
    """Inspect only the newest attempt IDs, projecting the receipt JSON path in SQL."""
    rows = TaskAttempt.objects.order_by("-pk").values("pk", "result__skill_assurance")[:_RECENT_ATTEMPTS]
    result = []
    for row in rows:
        assurance = skill_assurance({"skill_assurance": row["result__skill_assurance"]})
        if assurance is not None:
            result.append((row["pk"], assurance))
        if len(result) == _SKILL_COUNT:
            break
    return tuple(result)


def skill_bundle(attempt: "TaskAttempt") -> tuple[tuple[str, ...], bool]:
    """*attempt*'s recorded bundle, and whether an empty one is a fault.

    An attempt resolves a bundle by construction, so an empty one is a fault
    worth showing.
    """
    names = tuple(str(name) for name in (attempt.skills_loaded or []))
    return names, not names
