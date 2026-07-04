"""Prompt construction for the ``under_load`` behavioural-drift lane.

The clean-room lane (the default) loads one skill into an empty context to
isolate that skill's effect. The ``under_load`` lane does the opposite: it
reproduces the conditions a drift actually occurs under — the (near-)full skill
bundle as the system prompt plus an injected polluted ``context_preamble`` folded
into the user prompt — so a rule that survives clean-room but drifts in a real
session is caught.

The bundle is capped at :data:`_BUNDLE_CHAR_BUDGET` so a full bundle + a realistic
preamble + a multi-tool scenario (``Agent`` + ``Task`` schemas) fits the model's
200k-token input window instead of 400ing with "Prompt too long" before the model
runs. The cap keeps the scenario's own ``agent_path`` skill, the ``rules`` skill,
and every small canonical-source skill (smallest-first fill); only the largest
peripheral skills shed. The realistic-overload condition is preserved — the bundle
still dwarfs the preamble.

Two seams keep :mod:`teatree.eval.api_runner` thin (it is at its module-LOC
cap): :func:`build_system_prompt` resolves the lane-correct system prompt
(one skill + live-env framing, or the budgeted bundle + bundle framing), and
:func:`build_user_prompt` prepends the polluted preamble to the scenario prompt.

HARD SDK CONSTRAINT: ``claude_agent_sdk.query(prompt, options)`` is
user-turns-only — it accepts no pre-seeded assistant/tool-result turns — so the
pollution lives in the prompt TEXT, never as a faked multi-turn history.
"""

from pathlib import Path

from teatree.eval.models import UNDER_LOAD_LANE, EvalSpec
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING, SKILL_BUNDLE_FRAMING

#: ``skills/`` sits next to ``src/`` in the teatree tree; resolve it from this
#: module's path (the same backwards-edge convention discovery follows) so the
#: bundle loader never reaches up into a higher-level skill-loading module.
SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"

#: A skill-bundle entry is one ``## ``-separated section header naming the skill,
#: so the model can tell where one skill's rules end and the next begin.
_SKILL_BUNDLE_SEPARATOR = "\n\n"

#: Deterministic CHAR budget for the assembled under_load skill bundle. The lane
#: sends the WHOLE shipped bundle as the system prompt, which has grown to ~674k
#: chars (~168k tokens). The model's 200k-token (~800k-char) input window must ALSO
#: fit the polluted ``context_preamble`` (≤~40k chars), the bundle/live-env framing,
#: the tool-definition schemas (the heaviest scenarios add ``Agent`` + ``Task``,
#: several thousand tokens each), and the bundled CLI's base harness prompt — so a
#: full bundle + a realistic preamble + a multi-tool scenario blows the window and
#: the request 400s with "Prompt too long" BEFORE the model runs (run 27879918345:
#: read_canonical 3/3 and team_mode 2/3 never executed — a meaningless RED that
#: tests nothing). This budget caps the bundle so the assembled prompt fits with
#: headroom; the bundle still dwarfs the preamble, so the realistic-overload
#: condition is preserved. ~600k chars (~150k tokens) leaves ~50k tokens for
#: preamble + tools + base + response.
_BUNDLE_CHAR_BUDGET = 600_000

#: Skills the budgeted bundle ALWAYS keeps (on top of the scenario's ``agent_path``
#: skill): ``rules`` is the cross-cutting invariant surface that every real session
#: loads, so it is the single most important source of the cross-rule competition
#: the lane reproduces. Keeping it guarantees a scenario whose ``agent_path`` is a
#: DIFFERENT skill (e.g. ``wip``) still runs under the rules-skill load.
_ALWAYS_KEEP_SKILLS: frozenset[str] = frozenset({"rules"})


def _skill_sections(skills_dir: Path) -> list[tuple[str, str]]:
    """``(name, framed_section)`` for every non-empty shipped ``skills/<name>/SKILL.md``, sorted by name."""
    sections: list[tuple[str, str]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        body = skill_md.read_text(encoding="utf-8").strip()
        if not body:
            continue
        sections.append((skill_md.parent.name, f"## skill: {skill_md.parent.name}\n\n{body}"))
    return sections


def load_skill_bundle(*, skills_dir: Path = SKILLS_DIR) -> str:
    """Concatenate every shipped ``skills/<name>/SKILL.md`` into one bundle.

    Each skill's body is prefixed with a ``## skill: <name>`` header so the
    bundle reads as the model's complete, multi-skill operating ruleset (the
    drift-inducing overload condition). Skills are sorted for a deterministic,
    reproducible bundle. A skill directory with no ``SKILL.md`` is skipped.
    """
    return _SKILL_BUNDLE_SEPARATOR.join(section for _, section in _skill_sections(skills_dir))


def load_budgeted_skill_bundle(
    *,
    keep_skill: str | None = None,
    char_budget: int = _BUNDLE_CHAR_BUDGET,
    skills_dir: Path = SKILLS_DIR,
) -> str:
    """Concatenate the shipped skill bundle, deterministically capped at *char_budget*.

    The whole bundle outgrew the model's input window, so a full bundle plus a
    realistic ``context_preamble`` plus a multi-tool scenario overflows and the
    request 400s before the model runs. This caps the bundle while preserving the
    realistic-overload condition:

    *   ``keep_skill`` (the scenario's ``agent_path`` skill — the rule UNDER TEST)
        is NEVER dropped: it leads the bundle and is always present.
    *   The remaining skills are added SMALLEST-first until the next one would
        exceed the budget, then SKIPPED (not truncated mid-skill — a half-skill is
        worse context than a whole one omitted). Smallest-first keeps the MAXIMUM
        NUMBER of skills, so the realistic cross-rule competition is preserved and
        only the few largest tail skills are shed. Crucially it also keeps the small
        CANONICAL-SOURCE skills a scenario may need to read (e.g. ``loops``, the
        team-role-split source the read_canonical scenario tests) — a largest-first
        cap would drop them and silently remove the very requirement under test.

    Selection is deterministic (stable sort on ``(size, name)``), so a given
    catalog produces a byte-identical bundle every run. When the whole bundle
    already fits (``char_budget`` not exceeded) every skill is included, so a small
    catalog is unaffected.
    """
    sections = _skill_sections(skills_dir)
    by_name = dict(sections)
    chosen: list[str] = []
    used = 0
    sep = len(_SKILL_BUNDLE_SEPARATOR)

    def _add(name: str) -> None:
        nonlocal used
        chosen.append(name)
        used += len(by_name[name]) + (sep if len(chosen) > 1 else 0)

    # Pinned skills (the rule-under-test plus the always-keep set) lead the bundle
    # and are never subject to the budget cut — smallest pinned first for stability.
    pinned = {name for name in ({keep_skill} | set(_ALWAYS_KEEP_SKILLS)) if name and name in by_name}
    for name in sorted(pinned, key=lambda n: (len(by_name[n]), n)):
        _add(name)
    # The rest are filled in SMALLEST-first so the maximum number of skills (and the
    # small canonical-source skills) survive; only the largest tail is shed.
    remaining = sorted(
        ((name, section) for name, section in sections if name not in pinned),
        key=lambda item: (len(item[1]), item[0]),
    )
    for name, section in remaining:
        if used + sep + len(section) > char_budget:
            continue
        _add(name)
    # Re-emit in the catalog's canonical (name-sorted) order so the bundle reads the
    # same way the unbudgeted bundle does, not in size order.
    return _SKILL_BUNDLE_SEPARATOR.join(by_name[name] for name in sorted(chosen))


def _agent_path_skill_name(agent_path: str) -> str | None:
    """The ``skills/<name>/SKILL.md`` skill name from a spec's ``agent_path``, or ``None``."""
    parts = Path(agent_path).parts
    if "skills" in parts:
        idx = parts.index("skills")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def build_system_prompt(spec: EvalSpec, *, clean_room_prompt: str, skills_dir: Path = SKILLS_DIR) -> str:
    """Return the lane-correct system prompt for *spec*.

    ``clean_room_prompt`` is the single-skill prompt the runner already builds
    (``load_agent_definition(...) + LIVE_ENV_FRAMING``). For the ``under_load``
    lane this is replaced by the skill bundle framed with
    :data:`SKILL_BUNDLE_FRAMING` and the same live-env framing; every other lane
    is unchanged, so a clean-room run is byte-identical. The bundle is capped at
    :data:`_BUNDLE_CHAR_BUDGET` (the scenario's own ``agent_path`` skill always
    kept) so a full bundle + a realistic preamble + a multi-tool scenario fits the
    model's input window instead of 400ing before the model runs.
    """
    if spec.lane != UNDER_LOAD_LANE:
        return clean_room_prompt
    bundle = load_budgeted_skill_bundle(
        keep_skill=_agent_path_skill_name(spec.agent_path),
        skills_dir=skills_dir,
    )
    return SKILL_BUNDLE_FRAMING + bundle + LIVE_ENV_FRAMING


def build_user_prompt(spec: EvalSpec) -> str:
    """Return the user prompt for *spec*, folding any ``context_preamble`` in.

    The polluted preamble is prepended to the scenario prompt as plain text (the
    SDK user-turns-only constraint). A spec with no preamble — every clean-room
    spec — returns its prompt unchanged, so a clean-room run is byte-identical.
    """
    if not spec.context_preamble:
        return spec.prompt
    return f"{spec.context_preamble}\n\n{spec.prompt}"
