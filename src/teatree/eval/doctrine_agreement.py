"""Doctrine and the eval corpus must grade the same command (#4137).

The doctrine moved and the corpus did not. ``skills/ship/SKILL.md`` § 4a made
``t3 push`` the one supported push path from the worker container; three matchers
kept asserting the pre-#3949 ``git push`` spelling, so for months an agent that
followed current doctrine EXACTLY was graded wrong. Nothing could see it: the skill
is correct on its own, each matcher is correct on its own, and the defect is the
relationship between them.

Both halves of the agreement are derived here from ONE structure — the *seam
migration*: a fenced doctrine block whose forbidden form and mandated form are
different PROGRAMS. That is the class #4137 belongs to (doctrine moved the action
from ``git`` to ``t3``), and restricting to it is what makes the property honest:

*   A same-program pair is an ARGUMENT-level distinction — a different emoji, a
    ``--draft`` flag, a different subcommand. A positive matcher legitimately spans
    both sides of one, so treating those as violations produced only false positives.
*   A cross-program pair cannot be spanned by accident. A matcher that demands the
    forbidden program while admitting none of the mandated forms is asserting the
    retired spelling, full stop.

Extraction is deliberately literal about the markers the skills already use: a
``FORBIDDEN`` marker on a line inside a shell fence is the forbidden form, an
uncommented sibling line is a mandated form, and only lines opening with a real
executable token count — so prose and tool-call pseudo-code (``Edit(...)``,
``AskUserQuestion(...)``) are never mistaken for commands.
"""

import dataclasses
import re
from pathlib import Path

from teatree.eval.discovery import DEFAULT_SKILLS_DIR, SCENARIOS_DIR
from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import AnyOf, EvalSpec, Matcher

#: The marker a shipped skill puts on a command line it forbids.
FORBIDDEN_MARKER = "FORBIDDEN"

#: Fence languages whose bodies are read as shell. An untagged fence counts —
#: several doctrine blocks omit the tag.
_SHELL_FENCE_LANGS = frozenset({"bash", "sh", "shell", "console", ""})

#: The executables a doctrine block can mandate or forbid. An allowlist rather
#: than "any first word" so a prose line, a tool-call pseudo-call and a diff
#: fragment can never be read as a command.
_EXECUTABLES = frozenset(
    {
        "bash",
        "curl",
        "docker",
        "export",
        "gh",
        "git",
        "glab",
        "npm",
        "prek",
        "pytest",
        "python",
        "python3",
        "ruff",
        "sed",
        "t3",
        "uv",
    }
)

_FENCE = re.compile(r"^```(\w*)\s*$")
_TRAILING_COMMENT = re.compile(r"\s+#.*$")


@dataclasses.dataclass(frozen=True, slots=True)
class SeamMigration:
    """One doctrine block that moved a command onto a DIFFERENT program."""

    skill: str
    mandated: tuple[str, ...]
    forbidden: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class StaleMatcher:
    """A positive matcher demanding a command its own doctrine forbids."""

    scenario: str
    skill: str
    pattern: str
    forbidden: str

    def __str__(self) -> str:
        return f"{self.scenario} [{self.skill}] positive {self.pattern!r} demands {self.forbidden!r}"


@dataclasses.dataclass(frozen=True, slots=True)
class UnpinnedMandate:
    """A mandated command no scenario graded against its own doctrine pins."""

    skill: str
    command: str

    def __str__(self) -> str:
        return f"{self.skill}: {self.command!r} is pinned by no scenario"


def _command_on_line(line: str) -> str:
    """The shell command a doctrine line carries, or ``""`` when it carries none."""
    body = line.strip()
    if body.startswith("#"):
        body = body.lstrip("#").strip()
    body = _TRAILING_COMMENT.sub("", body).strip()
    head = body.split(" ", 1)[0] if body else ""
    return body if head in _EXECUTABLES else ""


def _shell_fences(text: str) -> list[list[str]]:
    fences: list[list[str]] = []
    current: list[str] | None = None
    lang = ""
    for raw in text.split("\n"):
        marker = _FENCE.match(raw.strip())
        if marker is None:
            if current is not None:
                current.append(raw)
            continue
        if current is None:
            lang = marker.group(1)
            current = []
        else:
            if lang in _SHELL_FENCE_LANGS:
                fences.append(current)
            current = None
    return fences


def _program(command: str) -> str:
    return command.split(" ", 1)[0]


def seam_migrations(skill: str, text: str) -> tuple[SeamMigration, ...]:
    """Every block in *text* that forbids one program while mandating another."""
    blocks: list[SeamMigration] = []
    for fence in _shell_fences(text):
        mandated: list[str] = []
        forbidden: list[str] = []
        for raw in fence:
            command = _command_on_line(raw)
            if not command:
                continue
            if FORBIDDEN_MARKER in raw:
                forbidden.append(command)
            elif not raw.strip().startswith("#"):
                mandated.append(command)
        programs = {_program(c) for c in mandated}
        migrated = tuple(c for c in forbidden if _program(c) not in programs)
        if migrated and mandated:
            blocks.append(SeamMigration(skill=skill, mandated=tuple(mandated), forbidden=migrated))
    return tuple(blocks)


def shipped_seam_migrations(skills_dir: Path = DEFAULT_SKILLS_DIR) -> tuple[SeamMigration, ...]:
    """Every seam migration declared by a shipped ``skills/*/SKILL.md``."""
    return tuple(
        block
        for path in sorted(skills_dir.glob("*/SKILL.md"))
        for block in seam_migrations(f"skills/{path.parent.name}/SKILL.md", path.read_text(encoding="utf-8"))
    )


def shipped_specs(scenarios_dir: Path = SCENARIOS_DIR) -> tuple[EvalSpec, ...]:
    """The shipped scenario catalog, without the installed-overlay contributions.

    Doctrine ships in this repo, so the corpus that must agree with it is this
    repo's — an overlay's private catalog cannot make a shipped matcher honest.
    """
    return tuple(spec for path in sorted(scenarios_dir.glob("*.yaml")) for spec in load_eval_yaml(path))


def _positive_command_matchers(spec: EvalSpec) -> tuple[Matcher, ...]:
    """The matchers that require the agent to RUN a shell command."""
    positives: list[Matcher] = []
    for item in spec.matchers:
        if isinstance(item, AnyOf):
            positives.extend(item.alternatives)
        elif isinstance(item, Matcher) and item.kind == "positive":
            positives.append(item)
    return tuple(m for m in positives if m.tool == "Bash" and "command" in m.arg_path)


def _matches(matcher: Matcher, command: str) -> bool:
    if matcher.operator == "~":
        return re.search(matcher.value, command) is not None
    return matcher.value in command


def stale_matchers(
    specs: tuple[EvalSpec, ...],
    migrations: tuple[SeamMigration, ...],
) -> tuple[StaleMatcher, ...]:
    """Positive matchers demanding a command the scenario's own doctrine retired.

    A matcher that admits ANY mandated form is not stale — it spans the migration,
    which is what a correctly-widened matcher looks like. Only one that matches the
    retired program and NO mandated form is asserting the pre-migration spelling.
    """
    by_skill = _migrations_by_skill(migrations)
    found: list[StaleMatcher] = []
    for spec in specs:
        mandated, forbidden = by_skill.get(spec.agent_path, ((), ()))
        if not forbidden:
            continue
        for matcher in _positive_command_matchers(spec):
            if any(_matches(matcher, c) for c in mandated):
                continue
            found.extend(
                StaleMatcher(scenario=spec.name, skill=spec.agent_path, pattern=matcher.value, forbidden=command)
                for command in forbidden
                if _matches(matcher, command)
            )
    return tuple(found)


def unpinned_mandates(
    specs: tuple[EvalSpec, ...],
    migrations: tuple[SeamMigration, ...],
) -> tuple[UnpinnedMandate, ...]:
    """Mandated commands no scenario graded against the same doctrine pins."""
    matchers_by_skill: dict[str, list[Matcher]] = {}
    for spec in specs:
        matchers_by_skill.setdefault(spec.agent_path, []).extend(_positive_command_matchers(spec))
    return tuple(
        UnpinnedMandate(skill=skill, command=command)
        for skill, (mandated, _forbidden) in _migrations_by_skill(migrations).items()
        for command in mandated
        if not any(_matches(m, command) for m in matchers_by_skill.get(skill, []))
    )


def _migrations_by_skill(
    migrations: tuple[SeamMigration, ...],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Per skill, the union of its mandated and forbidden forms.

    Unioned across blocks rather than compared per block: a skill states the same
    seam in more than one place (``ship`` § 4a mandates ``t3 push``, a later block
    forbids a post-review push), and a matcher that satisfies the seam anywhere in
    the doctrine satisfies it.
    """
    merged: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for block in migrations:
        mandated, forbidden = merged.get(block.skill, ((), ()))
        merged[block.skill] = (mandated + block.mandated, forbidden + block.forbidden)
    return merged
