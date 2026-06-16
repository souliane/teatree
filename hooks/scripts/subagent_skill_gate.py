"""Sub-agent skill-loading enforcement for the ``TaskCreated`` fan-out gate.

Split out of ``hook_router.py`` by concern (module health). The teatree skill
injection reaches the MAIN agent only; a sub-agent spawned via the harness
Workflow/Task fan-out starts BLANK — it has only its task prompt and lacks the
``Skill`` tool, so it never loads the t3 / overlay lifecycle skills. The
``TaskCreated`` gate therefore cannot be satisfied by what the PARENT session
loaded (that state does not transfer to the blank sub-agent); it is satisfied
only when the DISPATCH PROMPT itself instructs the sub-agent to load the skill.

This module owns three pieces.

``required_skills_for_task`` derives the UN-DERIVABLE ROOTS a fanned-out task
must name: the lifecycle skill detected from the description plus the active
overlay's companion skills for that lifecycle (every lifecycle, not just
``review``). It does NOT expand the transitive ``requires``/``companions``
closure — the ``Skill`` tool pulls each root's dependencies itself, so demanding
the whole closure over-blocks a dispatch that correctly names only the roots
(e.g. a reviewer dispatch naming ``/t3:review`` need not also enumerate
``code``/``workspace``/``platforms``/``architecture-design``). A trivial or
ambiguous task (``fix the typo``, ``push the branch``, ``investigate the build``)
yields no demand at all — see ``_task_is_trivial``.

``task_references_skill`` tests whether a task prompt already instructs the
sub-agent to load a given skill: a ``/t3:<name>`` / ``/<name>`` token, a
``<name>/SKILL.md`` path reference, or a ``load the <name> skill`` / ``Skill
tool`` instruction naming it — but a NEGATED mention (``do not load the code
skill``, ``skip the ship skill``) does NOT count as a reference.

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


def is_file_safe(path: Path) -> bool:
    """``path.is_file()`` that returns ``False`` instead of raising ``OSError``.

    A 255+ byte path segment makes ``is_file`` raise ``OSError`` ("File name
    too long"). The ``TaskCreated`` gate aborts on ANY handler stderr, so a
    pathological skill name in ``<session>.pending`` reaching a filesystem probe
    must degrade to "absent" rather than propagate — the name is then treated as
    unresolvable (fail open) instead of locking out task creation.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _skill_segment(name: str) -> str:
    """Return the bare skill segment of *name* (drops namespace + ``SKILL.md``).

    ``t3:review`` → ``review``; ``skills/code/SKILL.md`` → ``code``; ``code`` →
    ``code``. Pure and total — used to build the reference forms a prompt may
    use for the skill, independent of how the demand spelled it.
    """
    stripped = name.strip().rstrip("/")
    stripped = stripped.removesuffix("/SKILL.md")
    return stripped.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


# A trivial / ambiguous fan-out incidentally carries a lifecycle keyword
# (``fix the typo`` → ``fix``; ``push the branch`` → ``push``) but is not the
# substantive lifecycle dispatch the gate exists to govern. Forcing it to
# slash-list skills over-blocks. ``_TRIVIAL_TASK_RE`` recognizes the common
# trivial imperatives so the demand is suppressed for them — never-over-block.
_TRIVIAL_TASK_RE = re.compile(
    r"\b(?:typo|readme|comment|rename|whitespace|formatting|lint|"
    r"one-?liner|trivial|bump|wording|investigate|look into|figure out|find out|check why|why\b)\b",
    re.IGNORECASE,
)
# A substantive lifecycle dispatch reads like a real instruction, not a bare
# 3-word imperative (``push the branch``). At or below this word count a lone
# weak keyword is treated as trivial.
_MAX_TRIVIAL_WORDS = 3


def _task_is_trivial(description: str) -> bool:
    """Whether *description* is a trivial/ambiguous task the gate should not force.

    True when the text matches a trivial marker (``typo``/``readme``/``investigate``
    …) or is a bare short imperative (``push the branch``). A trivial task yields
    no skill demand so ``fix the typo in the README`` / ``push the branch`` /
    ``investigate why the build is broken`` are never forced to slash-list skills.
    A substantive dispatch (``review the open PR``, ``fix the broken parser``)
    still demands its lifecycle skill.
    """
    if _TRIVIAL_TASK_RE.search(description):
        return True
    return len(description.split()) <= _MAX_TRIVIAL_WORDS


def required_skills_for_task(description: str, search_dirs: list[Path]) -> list[str]:
    """The un-derivable ROOT skills a fanned-out task must name, from its DESCRIPTION.

    The lifecycle skill (``lifecycle_for_task_text``) unioned with the active
    overlay's companion skills for THAT lifecycle (every lifecycle, not just
    ``review``) — and NOTHING more. The transitive ``requires``/``companions``
    closure is deliberately NOT expanded: the ``Skill`` tool pulls each root's
    dependencies itself, so a dispatch that names the root passes even when it
    does not enumerate every transitive dep. Demanding the closure denied a
    reviewer dispatch that correctly named only ``/t3:review`` + the overlay
    review companion (over-block).

    A trivial/ambiguous task (:func:`_task_is_trivial`) yields ``[]`` so it is
    never forced to slash-list skills.

    Fail-open: any resolution failure (teatree not importable in this hook
    process, no lifecycle match, no configured overlay) yields ``[]`` so the
    gate degrades to the explicit pending demand alone — never a lockout.
    """
    if not description or _task_is_trivial(description):
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

        from teatree.skill_support.loading import SkillLoadingPolicy  # noqa: PLC0415

        index = build_trigger_index(search_dirs)
        lifecycle = SkillLoadingPolicy.lifecycle_for_task_text(description, trigger_index=index)
        if not lifecycle:
            return []
        roots = [lifecycle, *_overlay_companions_for_lifecycle(lifecycle)]
    except Exception:  # noqa: BLE001
        return []
    else:
        seen: set[str] = set()
        return [r for r in roots if not (r in seen or seen.add(r))]
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


# A reference whose OWN clause carries one of these markers is a NEGATED
# mention — ``do not load the code skill`` / ``skip the ship skill`` — and must
# NOT satisfy the gate, else a negation falsely clears the demand (under-block).
_NEGATION_RE = re.compile(
    r"(?:\bnot\b|n't\b|\bnever\b|\bno\b|\bskip\b|\bwithout\b|\bdon'?t\b|\bavoid\b|\bdrop\b)",
    re.IGNORECASE,
)
# Clause boundaries that RESET the negation scope: a negation in a PRIOR clause
# (``Do not skip steps. Load /t3:review``) does not negate this reference.
_CLAUSE_BOUNDARY_RE = re.compile(r"[.;\n]")


def task_references_skill(task_text: str, skill_name: str) -> bool:
    """Whether *task_text* instructs the sub-agent to load *skill_name*.

    The satisfaction test for the ``TaskCreated`` gate: a required skill is
    satisfied when the DISPATCH PROMPT references loading it (so the blank
    sub-agent is told to), NOT when the parent session happens to hold it. A
    NEGATED mention in the reference's own clause (``do not load the code
    skill``) is not a reference.
    """
    if not task_text or not skill_name:
        return False
    pattern = _reference_pattern(skill_name)
    return any(not _is_negated(task_text, match.start()) for match in pattern.finditer(task_text))


def _is_negated(text: str, match_start: int) -> bool:
    """Whether the reference at *match_start* sits in a negated clause.

    Scopes to the clause containing the match — the span after the last clause
    boundary (``.``/``;``/newline) before it — so a negation in a prior sentence
    does not falsely negate a genuine positive reference here.
    """
    boundaries = list(_CLAUSE_BOUNDARY_RE.finditer(text, 0, match_start))
    clause_start = boundaries[-1].end() if boundaries else 0
    return _NEGATION_RE.search(text, clause_start, match_start) is not None


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
        if is_file_safe(candidate):
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


def unreferenced_demand_reason(
    *,
    prompt: str,
    description: str,
    pending: list[str],
    search_dirs: list[Path],
    resolves: Callable[[str], bool],
) -> str:
    """The ``TaskCreated`` deny reason, or ``""`` when nothing is unreferenced.

    Owns the whole demand computation (roots + ``<session>.pending``, minus
    resolvable-and-already-referenced) AND its never-lockout fail-open: ANY
    internal error — notably a 255+ byte pending name making ``is_file`` raise
    ``OSError`` — returns ``""`` (allow) rather than propagating, since
    TaskCreated aborts on any handler stderr. The router handler stays a thin
    caller so the over-cap router nets smaller.
    """
    try:
        required = [*pending, *required_skills_for_task(description, search_dirs)]
        unreferenced = filter_unreferenced(prompt, required, resolves=resolves)
        return build_load_first_reason(unreferenced, search_dirs) if unreferenced else ""
    except Exception:  # noqa: BLE001 — never-lockout: fail OPEN, never abort TaskCreated.
        return ""
