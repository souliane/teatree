"""Skill-loading enforcement for the ``TaskCreated`` task-list gate.

Split out of ``hook_router.py`` by concern (module health). ``TaskCreated`` has
exactly ONE producer — the ``TaskCreate`` tool body — so this gate governs an
entry a session adds to its OWN task list; an ``Agent``/``Task``/Workflow
sub-agent fan-out never reaches the event, and no payload field marks a dispatch
(``docs/claude-code-internals.md`` carries the re-check grep). What the creating
session loaded does not travel to whoever later picks the entry up, so the gate
is satisfied only when the entry's own DESCRIPTION names the skills.

The demand is the parent session's ``<session>.pending`` set — the explicit
cwd/overlay-context skills (framework skill, overlay skill + companion skills)
the UserPromptSubmit hook recorded. There is no free-text scan of the task
description: which skills a task needs is expressed explicitly (the parent's
recorded demand), never inferred from prose.

This module owns four pieces.

``has_teammate_identity`` is the gate's SCOPE test, reading the fields that
carry the creating session's own ambient agent identity.

``task_references_skill`` tests whether a task description already instructs a
reader to load a given skill: a ``/t3:<name>`` / ``/<name>`` token, a
``<name>/SKILL.md`` path reference, or a ``load the <name> skill`` / ``Skill
tool`` instruction naming it — but a NEGATED mention (``do not load the code
skill``, ``skip the ship skill``) does NOT count as a reference.

``build_load_first_reason`` is the deny message listing the exact
``Read …/<name>/SKILL.md`` lines the description must carry.

``unreferenced_demand_reason`` is the whole demand computation + never-lockout
fail-open the router calls.

A bare sibling module (like ``mr_cli_fields`` / ``django_bootstrap``): the
router puts its own dir on ``sys.path`` so ``from subagent_skill_gate import …``
resolves both as the live hook and when imported as
``hooks.scripts.hook_router`` in tests. It NEVER imports the router back; the
search dirs and the ``resolves`` predicate are passed in.
"""

import re
from collections.abc import Callable, Mapping
from pathlib import Path

# The harness fills these from the CREATING session's ambient agent context
# (``agentName``/``teamName``), so they identify the author, not a target (#4216).
_TEAMMATE_IDENTITY_FIELDS = ("teammate_name", "team_name")


def has_teammate_identity(data: Mapping[str, object]) -> bool:
    """Whether a ``TaskCreated`` payload's creator carries a teammate identity.

    The gate's scope test, NOT a dispatch test: the event has one producer, so
    every payload is a task-list entry. A top-level session's entry carries
    neither field and is left alone — a demand that a plain todo name its skills
    is unsatisfiable by construction, which is how task tracking died silently.
    """
    return any(str(data.get(field) or "").strip() for field in _TEAMMATE_IDENTITY_FIELDS)


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
# (``Do not skip steps. Load /t3:review``; ``This is not optional: load
# /t3:review``) does not negate this reference. The colon resets only the scope
# BEFORE it, so a negation AFTER it (``Note: do not load X``) still governs —
# it fixes the emphatic-positive over-block without opening an under-block. The
# comma is deliberately NOT a boundary: it appears WITHIN a single negated
# imperative (``do not, under any circumstances, load X``), so resetting on it
# would let that negation escape (under-block).
_CLAUSE_BOUNDARY_RE = re.compile(r"[.;:\n]")


def task_references_skill(task_text: str, skill_name: str) -> bool:
    """Whether *task_text* instructs a reader to load *skill_name*.

    The satisfaction test for the ``TaskCreated`` gate: a required skill is
    satisfied when the task's own DESCRIPTION references loading it, NOT when
    the creating session happens to hold it. A NEGATED mention in the
    reference's own clause (``do not load the code skill``) is not a reference.
    """
    if not task_text or not skill_name:
        return False
    pattern = _reference_pattern(skill_name)
    return any(not _is_negated(task_text, match.start()) for match in pattern.finditer(task_text))


def _is_negated(text: str, match_start: int) -> bool:
    """Whether the reference at *match_start* sits in a negated clause.

    Scopes to the clause containing the match — the span after the last clause
    boundary (``.``/``;``/``:``/newline; NOT the comma, which appears within a
    single negated imperative) before it — so a negation in a prior clause does
    not falsely negate a genuine positive reference here.
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
    """The ``TaskCreated`` deny message listing the lines to ADD to the task.

    Lists one ``Read <abs>/SKILL.md`` line per unreferenced required skill so
    the entry itself carries the skill-loading instruction — what the creating
    session loaded does not travel to whoever picks the entry up.
    """
    add_lines = "\n".join(f"  Read {_skill_md_path(s, search_dirs)}" for s in unreferenced)
    slash = " ".join(f"/{_skill_segment(s)}" for s in unreferenced)
    return (
        "SKILL LOADING ENFORCEMENT (TaskCreated): this task's description does "
        f"not reference these required skills: {slash}. Add these lines to it so "
        f"whoever picks the task up loads them first:\n{add_lines}\n"
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
    pending: list[str],
    search_dirs: list[Path],
    resolves: Callable[[str], bool],
) -> str:
    """The ``TaskCreated`` deny reason, or ``""`` when nothing is unreferenced.

    Demands the task description reference every resolvable skill in the parent
    session's ``<session>.pending`` (the explicit cwd/overlay-context demand) —
    minus the ones already referenced. Owns its never-lockout
    fail-open: ANY internal error — notably a 255+ byte pending name making
    ``is_file`` raise ``OSError`` — returns ``""`` (allow) rather than
    propagating, since TaskCreated aborts on any handler stderr.
    """
    try:
        unreferenced = filter_unreferenced(prompt, pending, resolves=resolves)
        return build_load_first_reason(unreferenced, search_dirs) if unreferenced else ""
    except Exception:  # noqa: BLE001 — never-lockout: fail OPEN, never abort TaskCreated.
        return ""
