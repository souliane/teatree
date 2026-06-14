"""Prompt construction for the ``under_load`` behavioural-drift lane.

The clean-room lane (the default) loads one skill into an empty context to
isolate that skill's effect. The ``under_load`` lane does the opposite: it
reproduces the conditions a drift actually occurs under — the FULL skill bundle
as the system prompt plus an injected polluted ``context_preamble`` folded into
the user prompt — so a rule that survives clean-room but drifts in a real
session is caught.

Two seams keep :mod:`teatree.eval.sdk_runner` thin (it is at its module-LOC
cap): :func:`build_system_prompt` resolves the lane-correct system prompt
(one skill + live-env framing, or the whole bundle + bundle framing), and
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


def load_skill_bundle(*, skills_dir: Path = SKILLS_DIR) -> str:
    """Concatenate every shipped ``skills/<name>/SKILL.md`` into one bundle.

    Each skill's body is prefixed with a ``## skill: <name>`` header so the
    bundle reads as the model's complete, multi-skill operating ruleset (the
    drift-inducing overload condition). Skills are sorted for a deterministic,
    reproducible bundle. A skill directory with no ``SKILL.md`` is skipped.
    """
    sections: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        body = skill_md.read_text(encoding="utf-8").strip()
        if not body:
            continue
        sections.append(f"## skill: {skill_md.parent.name}\n\n{body}")
    return _SKILL_BUNDLE_SEPARATOR.join(sections)


def build_system_prompt(spec: EvalSpec, *, clean_room_prompt: str, skills_dir: Path = SKILLS_DIR) -> str:
    """Return the lane-correct system prompt for *spec*.

    ``clean_room_prompt`` is the single-skill prompt the runner already builds
    (``load_agent_definition(...) + LIVE_ENV_FRAMING``). For the ``under_load``
    lane this is replaced by the full skill bundle framed with
    :data:`SKILL_BUNDLE_FRAMING` and the same live-env framing; every other lane
    is unchanged, so a clean-room run is byte-identical.
    """
    if spec.lane != UNDER_LOAD_LANE:
        return clean_room_prompt
    return SKILL_BUNDLE_FRAMING + load_skill_bundle(skills_dir=skills_dir) + LIVE_ENV_FRAMING


def build_user_prompt(spec: EvalSpec) -> str:
    """Return the user prompt for *spec*, folding any ``context_preamble`` in.

    The polluted preamble is prepended to the scenario prompt as plain text (the
    SDK user-turns-only constraint). A spec with no preamble — every clean-room
    spec — returns its prompt unchanged, so a clean-room run is byte-identical.
    """
    if not spec.context_preamble:
        return spec.prompt
    return f"{spec.context_preamble}\n\n{spec.prompt}"
