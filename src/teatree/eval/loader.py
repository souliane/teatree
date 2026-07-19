"""Load eval specs from YAML into typed dataclasses.

Schema lives in ``evals/README.md`` and
``evals/scenarios/*.yaml``; the loader validates each spec at
load time and raises ``EvalSpecError`` with the offending file path so
spec authors can jump to the problem.

Supported operators: ``contains`` (substring match) and ``~`` (regex match).
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from teatree.agents.model_tiering import TIER_MODELS
from teatree.eval.cli_stub_fixture import KNOWN_CLI_STUBS
from teatree.eval.git_fixture import KNOWN_FIXTURES
from teatree.eval.matcher_vacuity import is_positive_anchor
from teatree.eval.models import (
    CLEAN_ROOM_LANE,
    DEFAULT_MAX_TURNS,
    MATCHER_KINDS,
    MATCHER_OPERATORS,
    PERMITTED_LANES,
    AnyOf,
    EvalSpec,
    ExpectItem,
    FinalStateMatcher,
    JudgeSpec,
    Matcher,
)

DEFAULT_AGENT_PATH = "skills/code/SKILL.md"
# Scenarios reference models by ABSTRACT TIER, not a concrete id. ``model`` is the
# escape-hatch concrete-id pin and defaults to unset (``""``); a tier/phase
# scenario resolves through teatree.agents.model_tiering.TIER_MODELS at run time.
DEFAULT_MODEL = ""
# DEFAULT_MAX_TURNS is the single canonical default, reused from
# teatree.eval.models (the data-layer owner of EvalSpec.max_turns's default).
DEFAULT_TOOLS: tuple[str, ...] = ("Bash",)
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"
DEFAULT_JUDGE_MAX_OUTPUT_TOKENS = 512

# Compiled FROM the single-source-of-truth operator set (teatree.eval.models) so the
# loader, the grader, and the dream synthesizer prompt cannot drift apart on which
# operators an `op "value"` expression may use.
_OP_PATTERN = re.compile(rf'^({"|".join(re.escape(op) for op in MATCHER_OPERATORS)})\s+"(.*)"$')


class EvalSpecError(ValueError):
    def __init__(self, path: Path, line: int | None, message: str) -> None:
        loc = f"{path}:{line}" if line is not None else str(path)
        super().__init__(f"{loc}: {message}")


def load_eval_yaml(path: Path, default_agent_path: str | None = None) -> list[EvalSpec]:
    text = path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        raise EvalSpecError(path, (line + 1) if line is not None else None, str(exc)) from exc
    if not isinstance(loaded, list) or not loaded:
        raise EvalSpecError(path, None, "expected a top-level YAML list with at least one spec")
    return [_parse_spec(entry, path, default_agent_path) for entry in loaded]


def _parse_spec(entry: object, path: Path, default_agent_path: str | None) -> EvalSpec:
    if not isinstance(entry, Mapping):
        raise EvalSpecError(path, None, f"each spec must be a mapping, got {type(entry).__name__}")
    spec_map: Mapping[str, Any] = {str(k): v for k, v in entry.items()}
    name = _required_str(spec_map, "name", path)
    scenario = _required_str(spec_map, "scenario", path)
    agent_path = str(spec_map.get("agent_path") or spec_map.get("agent") or default_agent_path or DEFAULT_AGENT_PATH)
    prompt = str(spec_map.get("prompt") or scenario)
    judge = _parse_judge(spec_map, name, path)
    expect = spec_map.get("expect")
    if expect is None and judge is not None:
        matchers: tuple[ExpectItem, ...] = ()
    elif not isinstance(expect, list) or not expect:
        raise EvalSpecError(path, None, f"spec {name!r}: `expect` must be a non-empty list")
    else:
        matchers = tuple(_parse_matcher(item, name, path) for item in expect)
    max_turns = _parse_max_turns(spec_map, name, path)
    tools = _parse_tools(spec_map, name, path)
    agent_sections = _parse_agent_sections(spec_map, name, path)
    available_skills = _parse_available_skills(spec_map, name, path)
    cli_stubs = _parse_cli_stubs(spec_map, name, path)
    single_action = _parse_bool(spec_map, "single_action", name, path)
    if single_action and not any(is_positive_anchor(m) for m in matchers):
        raise EvalSpecError(
            path,
            None,
            f"spec {name!r}: single_action requires at least one positive matcher "
            "(tool_call/any_of/final_state) — a negatives-only probe would vacuously pass on a cap",
        )
    return EvalSpec(
        name=name,
        scenario=scenario,
        agent_path=agent_path,
        prompt=prompt,
        matchers=matchers,
        source_path=path,
        model=str(spec_map.get("model") or DEFAULT_MODEL),
        tier=_parse_tier(spec_map, name, path),
        phase=_parse_phase(spec_map, name, path),
        max_turns=max_turns,
        tools=tools,
        fixture=_parse_fixture(spec_map, name, path),
        judge=judge,
        agent_sections=agent_sections,
        lane=_parse_lane(spec_map, name, path),
        context_preamble=str(spec_map.get("context_preamble") or ""),
        max_budget_usd=_parse_positive_float(spec_map, "max_budget_usd", name, path),
        watchdog_seconds=_parse_positive_float(spec_map, "watchdog_seconds", name, path),
        available_skills=available_skills,
        cli_stubs=cli_stubs,
        production_hooks=_parse_bool(spec_map, "production_hooks", name, path),
        single_action=single_action,
    )


def _parse_bool(entry: Mapping[str, Any], key: str, spec_name: str, path: Path) -> bool:
    """Parse an optional boolean flag, defaulting to ``False``.

    Absent means the flag is off (every existing scenario is unaffected); a present
    non-bool (a ``"true"`` string, a ``1``) is a spec error, not a silent truthy
    coercion, so an author who typo'd the value learns at load time.
    """
    raw = entry.get(key)
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `{key}` must be a boolean (true/false)")
    return raw


def _parse_fixture(entry: Mapping[str, Any], spec_name: str, path: Path) -> str:
    """Parse the optional ``fixture`` name, validated against the known set, or ``""``.

    Absent means "no fixture" (the neutral empty cwd); a present name outside
    :data:`teatree.eval.git_fixture.KNOWN_FIXTURES` is a spec error — a typo'd
    ``fixture:`` would otherwise silently yield an empty dir and the scenario would
    wander, the exact failure this loud check forecloses.
    """
    raw = entry.get("fixture")
    if raw is None:
        return ""
    if not isinstance(raw, str) or raw not in KNOWN_FIXTURES:
        permitted = ", ".join(sorted(KNOWN_FIXTURES))
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `fixture` must be one of {permitted}, got {raw!r}")
    return raw


def _parse_tier(entry: Mapping[str, Any], spec_name: str, path: Path) -> str:
    """Parse an optional ``tier`` (``frontier`` / ``balanced`` / ``cheap``), or ``""``.

    Validated against :data:`teatree.agents.model_tiering.TIER_MODELS` so a typo'd
    tier fails loud at load time, never silently resolves to the default tier.
    """
    raw = entry.get("tier")
    if raw is None:
        return ""
    if not isinstance(raw, str) or raw not in TIER_MODELS:
        permitted = ", ".join(sorted(TIER_MODELS))
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `tier` must be one of {permitted}, got {raw!r}")
    return raw


def _parse_phase(entry: Mapping[str, Any], spec_name: str, path: Path) -> str:
    """Parse an optional ``phase`` (a teatree FSM phase name), or ``""``.

    A phase resolves to its tier via ``DEFAULT_PHASE_MODELS`` at run time; an
    unmapped phase legitimately falls back to the default tier, so any non-empty
    string is accepted (only an empty/blank value is rejected).
    """
    raw = entry.get("phase")
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw.strip():
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `phase` must be a non-empty string")
    return raw.strip()


def _parse_judge(entry: Mapping[str, Any], spec_name: str, path: Path) -> JudgeSpec | None:
    raw = entry.get("judge")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `judge` must be a mapping")
    judge_map: Mapping[str, Any] = {str(k): v for k, v in raw.items()}
    rubric = judge_map.get("rubric")
    if not isinstance(rubric, str) or not rubric.strip():
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `judge.rubric` must be a non-empty string")
    raw_tokens = judge_map.get("max_output_tokens", DEFAULT_JUDGE_MAX_OUTPUT_TOKENS)
    if isinstance(raw_tokens, bool) or not isinstance(raw_tokens, int) or raw_tokens <= 0:
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `judge.max_output_tokens` must be a positive integer")
    return JudgeSpec(
        rubric=rubric,
        model=str(judge_map.get("model") or DEFAULT_JUDGE_MODEL),
        max_output_tokens=raw_tokens,
    )


def _parse_max_turns(entry: Mapping[str, Any], spec_name: str, path: Path) -> int:
    raw = entry.get("max_turns")
    if raw is None:
        return DEFAULT_MAX_TURNS
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `max_turns` must be a positive integer")
    return raw


def _parse_positive_float(entry: Mapping[str, Any], key: str, spec_name: str, path: Path) -> float | None:
    """Parse an optional positive ``float`` per-scenario cap override, or ``None``.

    Used for ``max_budget_usd`` / ``watchdog_seconds``: absent yields ``None``
    (defer to the run/lane default); a present non-positive or non-numeric value is
    a spec error so a fat-fingered ``0`` never silently tightens the cap to nothing.
    """
    raw = entry.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `{key}` must be a positive number")
    return float(raw)


def _parse_tools(entry: Mapping[str, Any], spec_name: str, path: Path) -> tuple[str, ...]:
    raw = entry.get("tools")
    if raw is None:
        return DEFAULT_TOOLS
    if not isinstance(raw, list) or not raw or not all(isinstance(t, str) and t for t in raw):
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `tools` must be a non-empty list of strings")
    return tuple(raw)


def _parse_lane(entry: Mapping[str, Any], spec_name: str, path: Path) -> str:
    raw = entry.get("lane")
    if raw is None:
        return CLEAN_ROOM_LANE
    if not isinstance(raw, str) or raw not in PERMITTED_LANES:
        permitted = ", ".join(sorted(PERMITTED_LANES))
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `lane` must be one of {permitted}, got {raw!r}")
    return raw


def _parse_agent_sections(entry: Mapping[str, Any], spec_name: str, path: Path) -> tuple[str, ...]:
    raw = entry.get("agent_sections")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) and s.strip() for s in raw):
        raise EvalSpecError(
            path, None, f"spec {spec_name!r}: `agent_sections` must be a non-empty list of section-heading strings"
        )
    return tuple(raw)


def _parse_available_skills(entry: Mapping[str, Any], spec_name: str, path: Path) -> tuple[str, ...]:
    """Parse the optional ``available_skills`` catalog-widening list, or ``()``.

    Mirrors :func:`_parse_agent_sections`: absent means "widen nothing" (every
    existing scenario is unaffected), present must be a non-empty list of
    non-blank strings — a fat-fingered empty list is a spec error, not a silent
    no-op, since an author who wrote the key meant to widen the catalog.
    """
    raw = entry.get("available_skills")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) and s.strip() for s in raw):
        raise EvalSpecError(
            path, None, f"spec {spec_name!r}: `available_skills` must be a non-empty list of skill-name strings"
        )
    return tuple(raw)


def _parse_cli_stubs(entry: Mapping[str, Any], spec_name: str, path: Path) -> tuple[str, ...]:
    """Parse the optional ``cli_stubs`` list of inert-CLI names, or ``()``.

    Mirrors :func:`_parse_available_skills`: absent means "stub nothing" (every
    existing scenario keeps an untouched ``PATH``); present must be a non-empty
    list of names drawn from :data:`teatree.eval.cli_stub_fixture.KNOWN_CLI_STUBS`
    — an unknown name (a typo, an unimplemented binary) is a spec error, not a
    silent no-op, since the sandbox would still error on that command.
    """
    raw = entry.get("cli_stubs")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) and s.strip() for s in raw):
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `cli_stubs` must be a non-empty list of CLI-name strings")
    unknown = [s for s in raw if s not in KNOWN_CLI_STUBS]
    if unknown:
        raise EvalSpecError(
            path, None, f"spec {spec_name!r}: unknown cli_stubs {unknown} (known: {sorted(KNOWN_CLI_STUBS)})"
        )
    return tuple(raw)


def _required_str(entry: Mapping[str, Any], key: str, path: Path) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalSpecError(path, None, f"required string field missing or empty: {key!r}")
    return value


def _parse_matcher(item: object, spec_name: str, path: Path) -> ExpectItem:
    if not isinstance(item, Mapping):
        raise EvalSpecError(path, None, f"spec {spec_name!r}: each `expect` entry must be a mapping")
    item_map: Mapping[str, Any] = {str(k): v for k, v in item.items()}
    if "any_of" in item_map:
        return _parse_any_of(item_map, spec_name, path)
    if "tool_call" in item_map:
        return _parse_positive(item_map, spec_name, path)
    if "no_tool_call_matching" in item_map:
        return _parse_negative(item_map, spec_name, path)
    if "final_state" in item_map:
        return _parse_final_state(item_map, spec_name, path)
    kinds = ", ".join(f"`{kind}`" for kind in MATCHER_KINDS)
    raise EvalSpecError(path, None, f"spec {spec_name!r}: expect entry must have one of {kinds}")


def _parse_final_state(item: Mapping[str, Any], spec_name: str, path: Path) -> FinalStateMatcher:
    operator, value = _parse_op_expr(str(item["final_state"]), spec_name, path)
    return FinalStateMatcher(operator=operator, value=value)


def _parse_any_of(item: Mapping[str, Any], spec_name: str, path: Path) -> AnyOf:
    branches = item["any_of"]
    if not isinstance(branches, list) or not branches:
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `any_of` must be a non-empty list of `tool_call` entries")
    alternatives: list[Matcher] = []
    for branch in branches:
        if not isinstance(branch, Mapping) or "tool_call" not in branch:
            raise EvalSpecError(
                path, None, f"spec {spec_name!r}: every `any_of` branch must be a `tool_call` entry (positive only)"
            )
        alternatives.append(_parse_positive({str(k): v for k, v in branch.items()}, spec_name, path))
    return AnyOf(alternatives=tuple(alternatives))


def _parse_positive(item: Mapping[str, Any], spec_name: str, path: Path) -> Matcher:
    tool = str(item["tool_call"]).strip()
    arg_key, op_expr = _single_args_entry(item, spec_name, path)
    operator, value = _parse_op_expr(op_expr, spec_name, path)
    return Matcher(kind="positive", tool=tool, arg_path=arg_key, operator=operator, value=value)


def _parse_negative(item: Mapping[str, Any], spec_name: str, path: Path) -> Matcher:
    inner = item["no_tool_call_matching"]
    if not isinstance(inner, Mapping) or len(inner) != 1:
        raise EvalSpecError(
            path,
            None,
            f'spec {spec_name!r}: `no_tool_call_matching` must hold exactly one `<tool>.<arg>: op "value"` entry',
        )
    inner_map: dict[str, Any] = {str(k): v for k, v in inner.items()}
    raw_key, op_expr = next(iter(inner_map.items()))
    if "." not in raw_key:
        raise EvalSpecError(path, None, f"spec {spec_name!r}: negative key must be `<tool>.<arg>`")
    tool, arg_path = raw_key.split(".", 1)
    operator, value = _parse_op_expr(str(op_expr), spec_name, path)
    guard_tool, guard_arg_path, guard_operator, guard_value = _parse_order_guard(item, spec_name, path)
    return Matcher(
        kind="negative",
        tool=tool,
        arg_path=arg_path,
        operator=operator,
        value=value,
        guard_tool=guard_tool,
        guard_arg_path=guard_arg_path,
        guard_operator=guard_operator,
        guard_value=guard_value,
    )


def _parse_order_guard(item: Mapping[str, Any], spec_name: str, path: Path) -> tuple[str, str, str, str]:
    """Parse the optional ``before_first`` order guard on a negative matcher.

    Shape: ``before_first: '<tool>.<arg> <op> "value"'`` (e.g.
    ``'Skill.skill ~ "t3-widget"'``). Present makes the negative order-aware — the
    forbidden call reds ONLY when it precedes the first guard call. Absent (the
    default) yields four empty strings, leaving the negative order-agnostic.
    """
    raw = item.get("before_first")
    if raw is None:
        return "", "", "", ""
    if not isinstance(raw, str) or not raw.strip():
        raise EvalSpecError(path, None, f"spec {spec_name!r}: `before_first` must be a non-empty string")
    key_part, _, op_part = raw.strip().partition(" ")
    if "." not in key_part or not op_part.strip():
        raise EvalSpecError(
            path, None, f'spec {spec_name!r}: `before_first` must read `<tool>.<arg> op "value"`, got {raw!r}'
        )
    guard_tool, guard_arg_path = key_part.split(".", 1)
    guard_operator, guard_value = _parse_op_expr(op_part, spec_name, path)
    return guard_tool, guard_arg_path, guard_operator, guard_value


def _single_args_entry(item: Mapping[str, Any], spec_name: str, path: Path) -> tuple[str, str]:
    args_entries = [(k, v) for k, v in item.items() if str(k).startswith("args.")]
    if len(args_entries) != 1:
        raise EvalSpecError(
            path,
            None,
            f'spec {spec_name!r}: `tool_call` entry needs exactly one `args.<path>: op "value"` line',
        )
    raw_key, value = args_entries[0]
    arg_path = str(raw_key).removeprefix("args.")
    return arg_path, str(value)


def _parse_op_expr(expr: str, spec_name: str, path: Path) -> tuple[str, str]:
    match = _OP_PATTERN.match(expr.strip())
    if not match:
        raise EvalSpecError(
            path,
            None,
            f'spec {spec_name!r}: operator must be `contains "..."` or `~ "..."`, got {expr!r}',
        )
    return match.group(1), match.group(2)
