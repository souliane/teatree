"""The coding-builder dispatch contract (split out of ``teatree.agents.prompt``).

A headless builder loses every loaded skill (rules § Sub-Agent Limitations), so
the architecture / code disciplines, the stack + overlay coding skills, and the
CI-parity verify step must reach it inline. This module owns that self-contained
concern — the forced-skill-load block + behavior-preservation + verify directive
(:func:`_coding_phase_directive`) — shared by ``build_task_prompt`` and the
coding branch of ``build_system_context`` so the contract cannot drift between
the two builder entry points. One-directional dependency: ``prompt`` imports
from here; nothing here imports ``prompt``.
"""

from teatree.agents.skill_injection import _ALWAYS_FULL_SKILLS, _explicit_load_name
from teatree.skill_support.loading import FRAMEWORK_SKILL_NAMES

_VERIFY_GATES_COMMAND = "t3 tool verify-gates"

# Auto-injected into a long-running maker brief (PR-12): user-visible liveness
# so a watchdog can tell "stuck" from "still working". The transport half is
# PR-14's worker heartbeat; this is the brief-level cue the sub-agent acts on.
_HEARTBEAT_DM_LINES: tuple[str, ...] = (
    "",
    "HEARTBEAT (long-running work): if this task runs long, emit a periodic user-visible",
    "progress DM (`t3 <overlay> notify send`) — report progress, not silence. A watchdog",
    "distinguishes a stuck run from a still-working one only if you check in.",
)

# The coding directive force-loads these two by name in its first lines, and
# ``rules`` is always embedded in full — so they are never re-listed in the
# resolved-stack skill-load block.
_DIRECTIVE_FORCED_SKILLS = frozenset({"architecture-design", "code"}) | _ALWAYS_FULL_SKILLS


def _stack_overlay_load_names(skills: list[str] | None, *, exclude: frozenset[str] = frozenset()) -> list[str]:
    """Return the ordered framework-then-overlay skill names to force-load.

    The resolved bundle minus the skills the directive already force-loads by
    name (``architecture-design`` / ``code``) and ``rules`` (always embedded in
    full). Framework skills (``ac-*`` / ``fastapi``) lead, then the overlay /
    remaining coding skills. Single source of truth for both the directive's
    load block and the system-context summary-suppression set so the two never
    disagree on which skills are force-loaded (#1368). *exclude* drops the
    phase's stage skills — a no-Skill-tool maker gets them embedded in full, so
    a "load via the Skill tool" directive it cannot act on must not list them.
    """
    forced = _DIRECTIVE_FORCED_SKILLS | exclude
    extra = [s for s in (skills or []) if _explicit_load_name(s) not in forced]
    framework = [s for s in extra if _explicit_load_name(s) in FRAMEWORK_SKILL_NAMES]
    overlay = [s for s in extra if _explicit_load_name(s) not in FRAMEWORK_SKILL_NAMES]
    ordered: list[str] = []
    for name in (*framework, *overlay):
        load_name = _explicit_load_name(name)
        if load_name not in ordered:
            ordered.append(load_name)
    return ordered


def _stack_skill_load_lines(skills: list[str] | None, *, exclude: frozenset[str] = frozenset()) -> list[str]:
    """Return the explicit "load the stack + overlay skills BEFORE code" block.

    A dispatched builder does not inherit the parent's loaded skills and
    auto-detect mis-fires when the worktree shape doesn't trip the detector
    (#1368). The resolved bundle already carries the stack's framework skill
    (``ac-django`` / ``ac-python`` / ``fastapi``) and the active overlay skill;
    this turns each into a verbatim "load via the Skill tool" instruction
    rather than letting it be demoted to the ignorable summary or left to
    auto-detect. When the stack cannot be determined (empty / unresolved
    bundle) a conservative default tells the builder to load its stack's
    coding skill itself, so a code-touching dispatch can never go out with no
    skill-load directive at all.
    """
    ordered = _stack_overlay_load_names(skills, exclude=exclude)
    if not ordered:
        return [
            "REQUIRED: before writing code, also load your stack's coding skill via the Skill tool",
            "(/ac-django for a Django repo, /ac-python for a Python repo) and the active overlay skill.",
            "The stack could not be auto-resolved at dispatch — load them yourself; do NOT skip this.",
            "",
        ]
    lines = ["REQUIRED: before writing code, also call the Skill tool for EACH of these stack/overlay skills:"]
    lines.extend(f"  - /{name}" for name in ordered)
    lines.extend(
        (
            "These carry the framework conventions and overlay-specific rules a dispatched builder",
            "does not auto-load — do NOT rely on auto-detect.",
            "",
        )
    )
    return lines


def _coding_phase_directive(
    skills: list[str] | None = None, *, stage_exclude: frozenset[str] = frozenset()
) -> list[str]:
    """Return the forced-load + behavior-preservation + verify directive lines.

    Symmetric to ``build_reviewer_dispatch_prompt``: a headless builder loses
    every loaded skill (rules § Sub-Agent Limitations), so the architecture /
    code disciplines, the stack + overlay coding skills, and the CI-parity
    verify step must reach it inline. Shared by ``build_task_prompt`` (the loop
    builder's work prompt) and the coding branch of ``build_system_context`` so
    the contract cannot drift between the two builder entry points. *skills* is
    the resolved bundle for the dispatch — its framework + overlay entries are
    surfaced as an explicit load block (#1368).
    """
    return [
        "REQUIRED: before writing code, call the Skill tool for /t3:architecture-design and /t3:code.",
        "Do this FIRST — these carry the design-first and TDD disciplines a dispatched builder",
        "does not auto-load.",
        "",
        *_stack_skill_load_lines(skills, exclude=stage_exclude),
        "BEHAVIOR PRESERVATION (non-negotiable): When you rewrite or REPLACE existing code, first",
        "enumerate every behavior/case the old code handled — especially safety/privacy/leak-gate",
        "coverage and the regression tests that pin it — and preserve each, or STOP and request input.",
        "NEVER silently narrow a gate; NEVER invert a must-block test to must-not-block; weakening a",
        "public-repo privacy gate is a BLOCKER, not a self-approved trade-off.",
        "",
        "NO AI SIGNATURE: Never add an AI/Claude signature or footer to commit messages OR to PR/issue",
        "bodies posted on the user's behalf (no 'Generated with Claude Code', no robot-emoji footer,",
        "no Co-Authored-By).",
        "",
        "OPEN QUESTIONS & ASSUMPTIONS: list every open question (solved or not) and every assumption not",
        "explicit from the spec in an 'Open questions & assumptions' section in BOTH the commit message",
        "body AND the PR description (status: decided-by-user / assumed / open). See skills/ship § 5.",
        "",
        f"VERIFY (CI-parity): before declaring done, run `{_VERIFY_GATES_COMMAND}`. It runs BOTH the",
        "commit-stage and push-stage hooks; a bare `prek run --all-files` SKIPS the push-stage gates",
        "(comment-density, doc-update, ensure-pr, the public-repo leak gate) that CI",
        "re-runs. Report its exit code as the green-proof — not a commit-stage-only run.",
        *_HEARTBEAT_DM_LINES,
    ]
