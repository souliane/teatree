"""Evaluate a GitHub Actions workflow's expressions and job gating, so tests assert outcomes per event."""

import math
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

CI_DAILY_CRON = "17 6 * * 1-6"
CI_WEEKLY_CRON = "17 6 * * 0"

type Value = str | float | bool | Mapping[str, Any] | None
type Functions = Mapping[str, Callable[..., Value]]

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|&&|\|\||[!(),])|(?P<word>[A-Za-z_][\w.-]*))"
)
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_STATUS_CALL = re.compile(r"\b(?:success|failure|cancelled|always)\(\)")
_LITERALS: dict[str, Value] = {"true": True, "false": False, "null": None}


def load(name: str = "ci.yml") -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(workflow: Mapping[str, Any]) -> dict[str, Any]:
    # `on` is a YAML 1.1 boolean, so safe_load keys the trigger block under True.
    return workflow.get("on") or workflow[True]


def _tokens(source: str) -> list[tuple[str, str]]:
    tokens, position = [], 0
    while source[position:].strip():
        match = _TOKEN.match(source, position)
        if match is None:
            msg = f"unsupported expression syntax at {source[position:]!r}"
            raise ValueError(msg)
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def _number(value: Value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool | int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip() or 0)
        except ValueError:
            return math.nan
    return math.nan


def _truthy(value: Value) -> bool:
    if isinstance(value, float):
        return not (value == 0 or math.isnan(value))
    return value is not None and value is not False and value != ""


def _equal(left: Value, right: Value) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if type(left) is type(right):
        return left == right
    return _number(left) == _number(right)


def _text(value: Value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def _format(template: Value, *args: Value) -> str:
    return re.sub(r"\{(\d+)\}", lambda match: _text(args[int(match.group(1))]), _text(template))


class _Evaluator:
    def __init__(self, source: str, context: Mapping[str, Any], functions: Functions) -> None:
        self._tokens = _tokens(source)
        self._position = 0
        self._context = context
        self._functions = {"format": _format, **functions}

    def evaluate(self) -> Value:
        value = self._or()
        if self._position != len(self._tokens):
            msg = f"trailing tokens {self._tokens[self._position :]}"
            raise ValueError(msg)
        return value

    def _peek(self) -> str | None:
        return self._tokens[self._position][1] if self._position < len(self._tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._tokens[self._position]
        self._position += 1
        return token

    def _accept(self, *operators: str) -> str | None:
        if self._peek() in operators:
            return self._take()[1]
        return None

    def _or(self) -> Value:
        left = self._and()
        while self._accept("||"):
            right = self._and()
            left = left if _truthy(left) else right
        return left

    def _and(self) -> Value:
        left = self._equality()
        while self._accept("&&"):
            right = self._equality()
            left = right if _truthy(left) else left
        return left

    def _equality(self) -> Value:
        left = self._unary()
        while operator := self._accept("==", "!="):
            left = _equal(left, self._unary()) == (operator == "==")
        return left

    def _unary(self) -> Value:
        if self._accept("!"):
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> Value:
        kind, text = self._take()
        if text == "(":
            value = self._or()
            self._accept(")")
            return value
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if self._accept("("):
            return self._call(text)
        return _LITERALS[text] if text in _LITERALS else self._lookup(text)

    def _call(self, name: str) -> Value:
        args: list[Value] = []
        while not self._accept(")"):
            args.append(self._or())
            self._accept(",")
        return self._functions[name](*args)

    def _lookup(self, path: str) -> Value:
        value: Any = self._context
        for part in path.split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        return value


def evaluate(source: str, context: Mapping[str, Any], functions: Functions | None = None) -> Value:
    return _Evaluator(source, context, functions or {}).evaluate()


def render(value: object, context: Mapping[str, Any]) -> Value:
    """A field that is one `${{ }}` keeps its type; text around expressions renders to a string."""
    text = str(value)
    whole = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", text, re.DOTALL)
    if whole and "${{" not in whole.group(1):
        return evaluate(whole.group(1), context)
    return _EXPRESSION.sub(lambda match: _text(evaluate(match.group(1), context)), text)


def condition_holds(condition: object, context: Mapping[str, Any], status: Mapping[str, bool]) -> bool:
    """Evaluate an `if:` the way the runner does: no status function means an implicit `success() &&`."""
    if isinstance(condition, bool):
        source = str(condition).lower()
    else:
        source = str(condition).strip()
        if wrapped := re.fullmatch(r"\$\{\{(.*)\}\}", source, re.DOTALL):
            source = wrapped.group(1)
    if not _STATUS_CALL.search(source):
        source = f"success() && ({source})"
    functions = {name: (lambda held=held: held) for name, held in status.items()}
    return _truthy(evaluate(source, context, functions))


def github_context(event_name: str, *, ref: str = "refs/heads/main", schedule: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {}
    if schedule is not None:
        event["schedule"] = schedule
    if event_name == "pull_request":
        event["pull_request"] = {"number": 42, "head": {"repo": {"full_name": "owner/repo", "fork": False}}}
    return {"event_name": event_name, "ref": ref, "run_id": "1001", "repository": "owner/repo", "event": event}


def _needs(job: Mapping[str, Any]) -> list[str]:
    declared = job.get("needs") or []
    return [declared] if isinstance(declared, str) else list(declared)


def _need_context(result: str, declared: Mapping[str, str]) -> dict[str, Any]:
    return {"result": result, "outputs": {} if result == "skipped" else dict(declared)}


def job_results(
    workflow: Mapping[str, Any],
    github: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Mapping[str, str]] | None = None,
    failing: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Each job's result for one event: a job that runs succeeds unless it is named in `failing`."""
    jobs: Mapping[str, Any] = workflow["jobs"]
    results: dict[str, str] = {}
    while len(results) < len(jobs):
        ready = [name for name, job in jobs.items() if name not in results and set(_needs(job)) <= results.keys()]
        if not ready:
            msg = f"dependency cycle among {sorted(set(jobs) - results.keys())}"
            raise ValueError(msg)
        for name in ready:
            needed = _needs(jobs[name])
            needs = {need: _need_context(results[need], (outputs or {}).get(need, {})) for need in needed}
            status = {
                "success": all(results[need] == "success" for need in needed),
                "failure": any(results[need] == "failure" for need in needed),
                "cancelled": False,
                "always": True,
            }
            context = {"github": github, "inputs": inputs or {}, "needs": needs}
            runs = condition_holds(jobs[name].get("if", True), context, status)
            results[name] = ("failure" if name in failing else "success") if runs else "skipped"
    return results


def ran(results: Mapping[str, str]) -> set[str]:
    return {name for name, result in results.items() if result != "skipped"}


def step_runs(
    step: Mapping[str, Any], context: Mapping[str, Any], *, earlier_step_failed: bool, cancelled: bool
) -> bool:
    status = {
        "success": not earlier_step_failed and not cancelled,
        "failure": earlier_step_failed,
        "cancelled": cancelled,
        "always": True,
    }
    return condition_holds(step.get("if", True), context, status)
