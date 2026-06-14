"""Load eval specs from YAML into typed dataclasses.

Schema lives in ``src/teatree/eval/README.md`` and
``tests/eval_lanes/scenarios/*.yaml``; the loader validates each spec at
load time and raises ``EvalSpecError`` with the offending file path so
spec authors can jump to the problem.

Supported operators: ``contains`` (substring match) and ``~`` (regex match).
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from teatree.eval.models import (
    CLEAN_ROOM_LANE,
    DEFAULT_MAX_TURNS,
    PERMITTED_LANES,
    AnyOf,
    EvalSpec,
    ExpectItem,
    FinalStateMatcher,
    JudgeSpec,
    Matcher,
)

DEFAULT_AGENT_PATH = "skills/code/SKILL.md"
DEFAULT_MODEL = "claude-sonnet-4-6"
# DEFAULT_MAX_TURNS is the single canonical default, reused from
# teatree.eval.models (the data-layer owner of EvalSpec.max_turns's default).
DEFAULT_TOOLS: tuple[str, ...] = ("Bash",)
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"
DEFAULT_JUDGE_MAX_OUTPUT_TOKENS = 512

_OP_PATTERN = re.compile(r'^(contains|~)\s+"(.*)"$')


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
    model = str(spec_map.get("model") or DEFAULT_MODEL)
    max_turns = _parse_max_turns(spec_map, name, path)
    tools = _parse_tools(spec_map, name, path)
    agent_sections = _parse_agent_sections(spec_map, name, path)
    lane = _parse_lane(spec_map, name, path)
    context_preamble = str(spec_map.get("context_preamble") or "")
    return EvalSpec(
        name=name,
        scenario=scenario,
        agent_path=agent_path,
        prompt=prompt,
        matchers=matchers,
        source_path=path,
        model=model,
        max_turns=max_turns,
        tools=tools,
        judge=judge,
        agent_sections=agent_sections,
        lane=lane,
        context_preamble=context_preamble,
    )


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
    raise EvalSpecError(
        path,
        None,
        f"spec {spec_name!r}: expect entry must have `tool_call`, `no_tool_call_matching`, `any_of`, or `final_state`",
    )


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
    return Matcher(kind="negative", tool=tool, arg_path=arg_path, operator=operator, value=value)


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
