"""Do eval scenarios and fixtures exercise commands that actually exist (#3566)?

An eval scenario asserts a behaviour; a ``_pass`` fixture asserts what the
correct trajectory looks like. Either can cite a ``t3 …`` command that does not
exist — and when it does, the scenario grades against a path the product cannot
take, so it is unreachable: it can never be satisfied by real behaviour, and it
teaches the graded model a command that isn't there. That is the same class as
a doc citing a removed subcommand, so it is checked the same way — by walking
the invocation against the LIVE typer command tree
(:func:`~teatree.eval.skill_command_validity.resolve_command_path`) rather than
against a hand-maintained list.

The reachability check is the meta-fix the eval-hygiene ticket asks for: a
scenario whose expectation names a nonexistent command fails HERE, at authoring
time, instead of silently grading nothing.
"""

import dataclasses
import json
import re
from collections.abc import Iterable
from pathlib import Path

from teatree.eval.skill_command_validity import resolve_command_path

#: A ``t3 …`` invocation anywhere in a YAML expectation or a fixture command
#: string — scenario/fixture text is not markdown, so backticks are not the
#: delimiter here; the run of shell-ish tokens after ``t3`` is.
_T3_INVOCATION = re.compile(r"\bt3 [a-z0-9][\w\- ]*")

_OVERLAY_PLACEHOLDER = "<overlay>"
_REPRESENTATIVE_OVERLAY = "teatree"


@dataclasses.dataclass(frozen=True)
class UnreachableCommand:
    """One ``t3 …`` invocation in a scenario/fixture that resolves to nothing."""

    source: str
    command: str


@dataclasses.dataclass(frozen=True)
class ReachabilityReport:
    unreachable: tuple[UnreachableCommand, ...]
    checked: int

    @property
    def ok(self) -> bool:
        return not self.unreachable

    def render_text(self) -> str:
        if self.ok:
            return f"scenario-reachability: {self.checked} `t3 …` invocation(s) all resolve."
        lines = [
            f"FAIL {u.source}: `{u.command}` does not resolve against the live CLI registry — "
            "the scenario grades a path the product cannot take"
            for u in self.unreachable
        ]
        lines.append(f"\nsummary: {len(self.unreachable)} unreachable reference(s) of {self.checked} checked")
        return "\n".join(lines)


def iter_t3_invocations(text: str) -> list[str]:
    """Every ``t3 …`` invocation in *text*, overlay placeholder resolved."""
    return [
        match.group(0).replace(_OVERLAY_PLACEHOLDER, _REPRESENTATIVE_OVERLAY).strip()
        for match in _T3_INVOCATION.finditer(text.replace(_OVERLAY_PLACEHOLDER, _REPRESENTATIVE_OVERLAY))
    ]


def _fixture_commands(path: Path) -> list[str]:
    """Every Bash command string recorded in a JSONL transcript fixture."""
    commands: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (event.get("message") or {}).get("content") or []
        commands.extend(
            str(block.get("input", {}).get("command", ""))
            for block in content
            if isinstance(block, dict) and block.get("name") == "Bash"
        )
    return commands


def _sources(scenarios_dir: Path, fixtures_dir: Path) -> Iterable[tuple[str, list[str]]]:
    for path in sorted(scenarios_dir.rglob("*.yaml")):
        yield str(path), iter_t3_invocations(path.read_text(encoding="utf-8"))
    for path in sorted(fixtures_dir.rglob("*.jsonl")):
        yield str(path), [inv for cmd in _fixture_commands(path) for inv in iter_t3_invocations(cmd)]


def validate_scenario_reachability(
    valid: set[str],
    groups: set[str],
    *,
    scenarios_dir: Path,
    fixtures_dir: Path,
) -> ReachabilityReport:
    """Check every ``t3 …`` cited by a scenario or fixture against the live registry."""
    unreachable: list[UnreachableCommand] = []
    checked = 0
    for source, invocations in _sources(scenarios_dir, fixtures_dir):
        for raw in invocations:
            checked += 1
            if resolve_command_path(raw, valid, groups) is None:
                unreachable.append(UnreachableCommand(source=source, command=raw))
    return ReachabilityReport(unreachable=tuple(unreachable), checked=checked)
