"""skill-command-validity: Tier-1 deterministic command-validity test (#550).

A behavioral scenario grades what an agent *does*; the skill-coverage lane grades
*whether* a skill ships an eval. This lane grades the skill *docs* themselves:
every backticked ``t3 …`` command a ``skills/<name>/SKILL.md`` (and its nested
``*.md`` references) documents must resolve against the LIVE CLI registry. A
SKILL.md that cites a ``t3`` command which no longer exists in the registry is
drift — the exact "no stale references" rule in CLAUDE.md — and FAILs the lane,
catching a stale skill doc after a CLI rename.

The engine is pure and dependency-inverted: it takes the registry as the
``(valid_paths, group_paths)`` argument pair (the ``teatree.cli_reference``
SSOT shape, ``{"t3 loop tick", …}``) rather than importing ``teatree.cli`` —
``teatree.eval`` must not reach UP into the CLI layer. The thin CLI lane
(``teatree.cli.eval.skill_command_lane``) builds the registry from the live
typer app and injects it.

This is a Layer-1 (deterministic, free, no model) test — no metering, no spend.
The parse + token-walk logic is the single chokepoint the skill-prose
static-invocation pytest gate (``tests/test_skill_t3_invocations.py``) also
consumes, so the regex and placeholder rules live in exactly one place.
"""

import dataclasses
import re
from collections.abc import Iterable
from pathlib import Path

# ``skills/`` sits next to ``src/`` in the teatree tree; resolve it from this
# module's path so the lane stays a leaf of the eval package (the same
# backwards-edge convention ``discovery`` / ``trigger_qa`` follow).
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"

# A backticked ``t3 …`` run command inside a markdown doc. Stops at the closing
# backtick; the captured words are normalized by the token-walker afterwards.
_T3_IN_BACKTICKS = re.compile(r"`(t3 [^`]+)`")

# Tokens that terminate the command path: an ASCII/unicode ellipsis (a generic
# CLI mention, not a specific command), an angle/brace placeholder, a shell var,
# an option/flag, a redirect/pipe, or a quoted arg value. A token matching this
# is where the concrete command path ends — anything after it is an arg/flag.
_PLACEHOLDER = re.compile(r"^(\.\.\.|…|<.*>|\$.*|--.*|-[A-Za-z]|\{.*\}|\|.*|>.*|\".*|'.*)$")


@dataclasses.dataclass(frozen=True)
class CommandViolation:
    """One backticked ``t3 …`` invocation in a skill doc that does not resolve."""

    skill: str
    doc: str
    command: str


@dataclasses.dataclass(frozen=True)
class CommandValidityReport:
    violations: tuple[CommandViolation, ...]
    checked: int

    @property
    def ok(self) -> bool:
        return not self.violations

    def render_text(self) -> str:
        if self.ok:
            return f"skill-command-validity: {self.checked} backticked `t3 …` invocation(s) all resolve."
        lines = [
            f"FAIL {v.skill} ({v.doc}): `{v.command}` does not resolve against the live CLI registry"
            for v in self.violations
        ]
        lines.append(f"\nsummary: {len(self.violations)} stale `t3 …` reference(s) of {self.checked} checked")
        return "\n".join(lines)


def iter_backticked_t3_commands(text: str) -> list[str]:
    """Every backticked ``t3 …`` run command in *text* (stripped of backticks)."""
    return [m.group(1).strip() for m in _T3_IN_BACKTICKS.finditer(text)]


def resolve_command_path(raw: str, valid: set[str], groups: set[str]) -> str | None:
    """Token-walk a backticked invocation against the command tree.

    Descends token by token while each extends a valid path; returns the deepest
    matched valid path, or ``None`` (drift) iff the deepest matched node is a
    **group** and the next non-placeholder token does NOT extend it to a valid
    child (a typo'd/removed subcommand). A token after a **leaf** (or a
    placeholder/flag anywhere) is a normal argument, not drift. A first token
    that is itself a placeholder (``t3 <overlay> …`` / ``t3 …``) leaves the
    matched node at the root group ``t3`` which is not a concrete command — it
    resolves to ``None`` (skipped, not a violation, by the caller).
    """
    toks = raw.split()
    if not toks or toks[0] != "t3":
        return None
    matched = "t3"
    for tok in toks[1:]:
        if _PLACEHOLDER.match(tok):
            break  # args/flags/placeholders begin — matched node stands
        nxt = f"{matched} {tok}"
        if nxt in valid:
            matched = nxt
            continue
        # `tok` does not extend `matched`. If `matched` is a group, the next word
        # was supposed to be a subcommand → drift. If it is a leaf, `tok` is a
        # positional argument → stop, matched stands.
        if matched in groups:
            return None
        break
    return matched if matched in valid else None


def _is_placeholder_only(raw: str) -> bool:
    """True for a generic CLI mention (``t3 …`` / ``t3 <overlay> …``).

    The first token after ``t3`` is a placeholder, so the invocation names no
    concrete command — it is documentation, never drift.
    """
    toks = raw.split()
    first_arg = toks[1] if len(toks) > 1 else None
    return first_arg is not None and bool(_PLACEHOLDER.match(first_arg))


def _iter_skill_docs(skills_dir: Path) -> Iterable[tuple[str, Path]]:
    if not skills_dir.is_dir():
        return
    for md in sorted(skills_dir.glob("*/**/*.md")):
        skill = md.relative_to(skills_dir).parts[0]
        yield skill, md


def validate_skill_commands(
    valid: set[str],
    groups: set[str],
    *,
    skills_dir: Path = DEFAULT_SKILLS_DIR,
) -> CommandValidityReport:
    """Validate every backticked ``t3 …`` in every skill doc against the registry.

    *valid* / *groups* are the live CLI registry sets (``command_paths`` /
    ``command_groups`` over the typer app). A backticked invocation that names a
    concrete command which does not resolve is a :class:`CommandViolation`. A
    generic placeholder mention (``t3 …`` / ``t3 <overlay> …``) is skipped — it
    names no concrete command. ``checked`` counts the concrete invocations
    examined (placeholders excluded).
    """
    violations: list[CommandViolation] = []
    checked = 0
    for skill, md in _iter_skill_docs(skills_dir):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw in iter_backticked_t3_commands(text):
            if _is_placeholder_only(raw):
                continue
            checked += 1
            if resolve_command_path(raw, valid, groups) is None:
                violations.append(CommandViolation(skill=skill, doc=md.relative_to(skills_dir).as_posix(), command=raw))
    return CommandValidityReport(violations=tuple(violations), checked=checked)
