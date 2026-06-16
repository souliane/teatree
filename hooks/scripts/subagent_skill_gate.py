"""Sub-agent skill-loading enforcement for the ``TaskCreated`` fan-out gate.

Split out of ``hook_router.py`` by concern (module health). The teatree skill
injection reaches the MAIN agent only; a sub-agent spawned via the harness
Workflow/Task fan-out starts BLANK — it has only its task prompt and lacks the
``Skill`` tool, so it never loads the t3 / overlay lifecycle skills. The
``TaskCreated`` gate therefore cannot be satisfied by what the PARENT session
loaded (that state does not transfer to the blank sub-agent); it is satisfied
only when the DISPATCH PROMPT itself instructs the sub-agent to load the skill.

This module owns three pieces.

``required_skills_for_task`` derives the lifecycle skill from the task
description, its transitive ``requires``/``companions`` closure, and the active
overlay's companion skills for EVERY lifecycle (not just ``review``).

``task_references_skill`` tests whether a task prompt already instructs the
sub-agent to load a given skill: a ``/t3:<name>`` / ``/<name>`` token, a
``<name>/SKILL.md`` path reference, or a ``load the <name> skill`` / ``Skill
tool`` instruction naming it.

``build_load_first_reason`` is the deny message listing the exact
``Read …/<name>/SKILL.md`` lines the orchestrator must embed in the dispatch.

A bare sibling module (like ``mr_cli_fields`` / ``django_bootstrap``): the
router puts its own dir on ``sys.path`` so ``from subagent_skill_gate import …``
resolves both as the live hook and when imported as
``hooks.scripts.hook_router`` in tests. It NEVER imports the router back; the
search dirs and the ``resolves`` predicate are passed in.
"""

import contextlib
import re
import sys
from collections.abc import Callable
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _skill_segment(name: str) -> str:
    """Return the bare skill segment of *name* (drops namespace + ``SKILL.md``).

    ``t3:review`` → ``review``; ``skills/code/SKILL.md`` → ``code``; ``code`` →
    ``code``. Pure and total — used to build the reference forms a prompt may
    use for the skill, independent of how the demand spelled it.
    """
    stripped = name.strip().rstrip("/")
    stripped = stripped.removesuffix("/SKILL.md")
    return stripped.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def required_skills_for_task(description: str, search_dirs: list[Path]) -> list[str]:
    """Skills a fanned-out task must load, derived from its DESCRIPTION.

    The lifecycle skill (``lifecycle_for_task_text``) plus its transitive
    ``requires``/``companions`` closure, unioned with the active overlay's
    companion skills for THAT lifecycle. Generalizes the former review-only
    seed: a ``code``/``e2e``/``test`` task demands the overlay's companions too,
    so an overlay's own code task demands its skill, not just a review task.

    Fail-open: any resolution failure (teatree not importable in this hook
    process, no lifecycle match, no configured overlay) yields ``[]`` so the
    gate degrades to the explicit pending demand alone — never a lockout.
    """
    if not description:
        return []

    scripts_dir = _PLUGIN_ROOT / "scripts"
    src_dir = _PLUGIN_ROOT / "src"
    added: list[str] = []
    for extra in (str(scripts_dir), str(src_dir)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
            added.append(extra)
    try:
        from lib.skill_loader import build_trigger_index  # noqa: PLC0415

        from teatree.skill_support.deps import resolve_companions  # noqa: PLC0415
        from teatree.skill_support.loading import SkillLoadingPolicy  # noqa: PLC0415

        index = build_trigger_index(search_dirs)
        lifecycle = SkillLoadingPolicy.lifecycle_for_task_text(description, trigger_index=index)
        if not lifecycle:
            return []
        seed = [lifecycle, *_overlay_companions_for_lifecycle(lifecycle)]
        resolved, _missing = resolve_companions(seed, index)
    except Exception:  # noqa: BLE001
        return []
    else:
        return resolved
    finally:
        for extra in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(extra)


def _overlay_companions_for_lifecycle(lifecycle: str) -> list[str]:
    """Active overlay's companion skills for *lifecycle*, or ``[]`` (fail-open).

    Calls ``OverlayConfig.get_lifecycle_companion_skills(lifecycle)`` so every
    lifecycle (``code``/``e2e``/``test``/``review``) unions the overlay's own
    skills — not only ``review``. Any import/resolution failure (teatree
    unimportable, no configured overlay) yields ``[]``.
    """
    try:
        from teatree.agents.skill_bundle import active_overlay_lifecycle_skills  # noqa: PLC0415

        return active_overlay_lifecycle_skills(lifecycle)
    except Exception:  # noqa: BLE001
        return []


def _reference_pattern(skill_name: str) -> re.Pattern[str]:
    """Compile the regex that matches an instruction to load *skill_name*.

    A skill is referenced when the prompt carries any of: a ``/t3:<seg>`` or
    ``/<seg>`` slash token, a ``<seg>/SKILL.md`` path reference (anchored to the
    skill's own dir so ``code`` does not match an unrelated ``…/SKILL.md``), or a
    ``load … <seg> … skill`` / ``Skill tool`` instruction naming the segment.
    The match is case-insensitive on the segment and word-boundary anchored so
    ``code`` never matches inside ``decode``.
    """
    seg = re.escape(_skill_segment(skill_name))
    return re.compile(
        rf"(?:/(?:t3:)?{seg}\b"  # /code or /t3:code slash token
        rf"|\b{seg}/SKILL\.md\b"  # code/SKILL.md path reference
        rf"|\bskill\s+tool\b.*?\b{seg}\b"  # "Skill tool … code"
        rf"|\b{seg}\b.*?\bskill\s+tool\b"  # "code … Skill tool"
        rf"|\bload\b.*?\b{seg}\b.*?\bskill\b"  # "load the code skill"
        rf"|\b{seg}\b\s+skill\b)",  # "code skill"
        re.IGNORECASE | re.DOTALL,
    )


def task_references_skill(task_text: str, skill_name: str) -> bool:
    """Whether *task_text* instructs the sub-agent to load *skill_name*.

    The satisfaction test for the ``TaskCreated`` gate: a required skill is
    satisfied when the DISPATCH PROMPT references loading it (so the blank
    sub-agent is told to), NOT when the parent session happens to hold it.
    """
    if not task_text or not skill_name:
        return False
    return _reference_pattern(skill_name).search(task_text) is not None


def _skill_md_path(skill_name: str, search_dirs: list[Path]) -> str:
    """Absolute ``…/<seg>/SKILL.md`` path for *skill_name*, for the deny lines.

    Resolves against the first *search_dir* that actually carries the skill so
    the orchestrator can paste a working ``Read`` line; falls back to the bare
    ``<seg>/SKILL.md`` shape when none does (the resolvable-filter upstream means
    this fallback is rarely reached).
    """
    seg = _skill_segment(skill_name)
    for directory in search_dirs:
        candidate = directory / seg / "SKILL.md"
        if candidate.is_file():
            return str(candidate)
    return f"{seg}/SKILL.md"


def build_load_first_reason(unreferenced: list[str], search_dirs: list[Path]) -> str:
    """The ``TaskCreated`` deny message listing the lines to ADD to the prompt.

    Lists one ``Read <abs>/SKILL.md`` line per unreferenced required skill so
    the orchestrator embeds skill-loading in the dispatch prompt — which is what
    guarantees the blank sub-agent is told to load skills.
    """
    add_lines = "\n".join(f"  Read {_skill_md_path(s, search_dirs)}" for s in unreferenced)
    slash = " ".join(f"/{_skill_segment(s)}" for s in unreferenced)
    return (
        "SKILL LOADING ENFORCEMENT (TaskCreated): this fanned-out sub-agent "
        "starts BLANK and will not load teatree skills unless its dispatch "
        "prompt tells it to. The prompt does not reference these required "
        f"skills: {slash}. Add these lines to the task description so the "
        f"sub-agent loads them first:\n{add_lines}\n"
        "(Disable with `t3 <overlay> gate skill-loading disable` or prefix the "
        "task with `[skip-skill-gate: <reason>]`.)"
    )


def filter_unreferenced(
    description: str,
    required: list[str],
    *,
    resolves: Callable[[str], bool],
) -> list[str]:
    """Required, RESOLVABLE skills the *description* does not yet reference.

    The demand the gate denies on. A skill drops out when it is unresolvable
    (stale/renamed — fail-open) or already referenced in the dispatch prompt.
    Order-preserving and deduped by bare segment.
    """
    seen: set[str] = set()
    demand: list[str] = []
    for name in required:
        seg = _skill_segment(name)
        if seg in seen or not resolves(name) or task_references_skill(description, name):
            continue
        seen.add(seg)
        demand.append(name)
    return demand
