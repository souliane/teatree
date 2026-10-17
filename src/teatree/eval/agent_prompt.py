"""The system prompt sent to an eval agent and hashed for verdict reuse."""

from pathlib import Path

from teatree.eval.context_budget import extract_sections
from teatree.eval.models import EvalSpec
from teatree.eval.production_hooks import teatree_root
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING
from teatree.eval.under_load import build_system_prompt

SKILL_CATALOG_FIXTURE_RELATIVE_PATH = ("evals", "fixtures", "skill_catalog")


def scenario_skill_catalog_roots(spec: EvalSpec) -> tuple[Path, ...]:
    if not spec.available_skills:
        return ()
    core_root = teatree_root().resolve()
    return tuple(
        root
        for root in spec.source_path.resolve().parents
        if root != core_root
        and (root / ".claude-plugin" / "plugin.json").is_file()
        and any((root / "skills" / name / "SKILL.md").is_file() for name in spec.available_skills)
    )


def resolve_agent_path(agent_path: str, spec_dir: Path | None = None) -> Path:
    resolved = Path(agent_path).expanduser()
    if not resolved.is_absolute():
        spec_roots = (spec_dir,) if spec_dir is not None else ()
        for root in (*spec_roots, Path.cwd(), teatree_root()):
            candidate = root / resolved
            if candidate.is_file():
                return candidate
    return resolved


def load_agent_definition(agent_path: str, agent_sections: tuple[str, ...] = (), spec_dir: Path | None = None) -> str:
    resolved = resolve_agent_path(agent_path, spec_dir)
    if not resolved.is_file():
        msg = f"Agent definition not found: {agent_path}"
        raise FileNotFoundError(msg)
    text = resolved.read_text(encoding="utf-8")
    if not text.strip():
        msg = f"Agent definition is empty: {resolved}"
        raise ValueError(msg)
    if agent_sections:
        return extract_sections(text, agent_sections)
    return text


def scenario_system_prompt(spec: EvalSpec) -> str:
    clean_room_prompt = load_agent_definition(spec.agent_path, spec.agent_sections, spec.source_path.parent)
    return build_system_prompt(spec, clean_room_prompt=clean_room_prompt + LIVE_ENV_FRAMING)
