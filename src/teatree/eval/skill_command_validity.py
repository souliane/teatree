"""skill-command-validity: Tier-1 deterministic command-validity eval (#550).

A behavioral scenario grades what an agent *does*; the skill-coverage lane grades
*whether* a skill ships an eval. This lane grades the repo's *prose docs*
themselves: every backticked ``t3 …`` command a doc in :data:`DOC_GLOBS` cites —
``skills/<name>/SKILL.md`` and its nested references, the ``agents/*.md`` role
briefs, ``BLUEPRINT.md``, and the ``docs/`` tree — must resolve against the LIVE
CLI registry. A doc that cites a ``t3`` command which no longer exists in the
registry is drift — the exact "no stale references" rule in CLAUDE.md — and FAILs
the lane, catching a stale doc after a CLI rename.

The engine is pure and dependency-inverted: it takes the registry as the
``(valid_paths, group_paths)`` argument pair (the ``teatree.cli_reference``
SSOT shape, ``{"t3 loop tick", …}``) rather than importing ``teatree.cli`` —
``teatree.eval`` must not reach UP into the CLI layer. The thin CLI lane
(``teatree.cli.eval.skill_command_lane``) builds the registry from the live
typer app and injects it.

This is a Layer-1 (deterministic, free, no model) eval — no metering, no spend.
The parse + token-walk logic is the single chokepoint the doc-prose
static-invocation pytest gate (``tests/test_skill_t3_invocations.py``) also
consumes, so the regex and placeholder rules live in exactly one place.
"""

import dataclasses
import re
from collections.abc import Iterable
from pathlib import Path

from teatree.eval.discovery import DEFAULT_SKILLS_DIR  # the one eval-leaf skills-dir resolver

#: The repo root the doc corpus is globbed from — ``skills/`` sits next to it.
DEFAULT_REPO_ROOT = DEFAULT_SKILLS_DIR.parent

#: Every prose corpus whose ``t3 …`` citations are gated. Only human-authored
#: docs: the source of truth for a command is the CLI itself, so validating docs
#: GENERATED from it (:data:`EXCLUDED_DOC_PREFIXES`) would be circular.
DOC_GLOBS: tuple[str, ...] = ("BLUEPRINT.md", "agents/*.md", "docs/**/*.md", "skills/*/**/*.md")

#: Repo-relative prefixes dropped from the corpus. ``docs/generated/`` is rendered
#: from the live command tree by the doc generators, and its box-drawn help tables
#: wrap a single command across several lines — a wrapped fragment is a rendering
#: artifact, never doc drift.
EXCLUDED_DOC_PREFIXES: tuple[str, ...] = ("docs/generated/",)

# A backticked ``t3 …`` run command inside a markdown doc. Stops at the closing
# backtick; the captured words are normalized by the token-walker afterwards.
_T3_IN_BACKTICKS = re.compile(r"`(t3 [^`]+)`")

# Tokens that terminate the command path: an ASCII/unicode ellipsis (a generic
# CLI mention, not a specific command), an angle/brace placeholder, a shell var,
# an option/flag, a redirect/pipe, or a quoted arg value. A token matching this
# is where the concrete command path ends — anything after it is an arg/flag.
_PLACEHOLDER = re.compile(r"^(\.\.\.|…|<.*>|\$.*|--.*|-[A-Za-z]|\{.*\}|\|.*|>.*|\".*|'.*)$")

# A command-path segment: the shape a real typer command name takes. A first
# token that is neither this nor a placeholder is not an invocation at all (the
# entry-point spec ``t3 = t3_bootstrap:main``), so it names no command to check.
_COMMAND_WORD = re.compile(r"^[a-z][a-z0-9_-]*$")

# A slash/pipe-joined enumeration of command words — the doc shorthand for
# several sibling subcommands (``t3 loop enable/disable``, ``t3 prompts
# list|render``). Each alternative is walked separately, so a real subcommand
# hiding inside an enumeration is validated rather than skipped.
_ALTERNATION = re.compile(r"^[a-z][a-z0-9_-]*(?:[|/][a-z][a-z0-9_-]*)+$")

# The `<overlay>` slot in a `t3 <overlay> <group> <sub>` doc template is not a
# free-text argument — it is the command-path segment every overlay-scoped `t3`
# invocation carries. It resolves to a concrete overlay at runtime, so validating
# the group+sub path requires substituting it with the representative overlay the
# #550 registry is assembled from (``teatree.cli._assemble_teatree_app`` builds
# the registry from the ``teatree`` overlay, so its ``t3 teatree …`` paths are
# what a ``t3 <overlay> …`` template must resolve against).
_REPRESENTATIVE_OVERLAY = "teatree"

# Overlay names the docs use as illustrative stand-ins. They are the same
# command-path slot as ``<overlay>`` — substituted, never skipped, so the
# group+sub path after them is still validated.
_EXAMPLE_OVERLAYS = frozenset({"acme", "example", "myoverlay"})

#: Backticked citations that legitimately do not resolve, each with the reason it
#: is exempt. Never a home for a genuinely wrong command — an entry that starts
#: resolving is stale and the anti-rot test demands its removal.
ALLOWED_NON_RESOLVING: dict[str, str] = {
    "t3 loops run": (
        "a NEGATIVE existence claim — BLUEPRINT states there is no such interval "
        "runner, so the citation is correct precisely because it does not resolve"
    ),
    "t3 overlay contract-check --compose <paths>": (
        "a real management command with no overlay proxy leaf, so the introspected "
        "tree cannot see it; exempted identically by tests/test_cli_command_literals_resolve.py"
    ),
    "t3 <overlay> tool run": (
        "the per-overlay `tool` group is registered dynamically from the overlay's "
        "own */hook-config/tool-commands.json, so it is absent from a registry built "
        "for an overlay that ships none"
    ),
}


@dataclasses.dataclass(frozen=True)
class CommandViolation:
    """One backticked ``t3 …`` invocation in a repo doc that does not resolve."""

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
        lines = [f"FAIL {v.doc}: `{v.command}` does not resolve against the live CLI registry" for v in self.violations]
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
    that is itself a placeholder (``t3 …``) leaves the matched node at the root
    group ``t3`` which is not a concrete command — it resolves to ``None``
    (skipped, not a violation, by the caller). The overlay slot of a
    ``t3 <overlay> …`` template is substituted with a concrete overlay upstream
    (:func:`_resolve_overlay_placeholder`), so its group+sub path is walked here.
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


def _resolve_overlay_placeholder(raw: str) -> str:
    """Substitute a leading overlay slot with the representative overlay.

    ``t3 <overlay> <group> <sub>`` is the shape of nearly every overlay-scoped
    ``t3`` example in the docs; the slot is a command-path segment, not a
    free-text argument, and an illustrative overlay NAME (``t3 acme …``) is the
    same slot spelled out. Resolving either to the concrete overlay the registry
    is built from lets the group+sub path be validated against the real command
    tree instead of being short-circuited by the leading placeholder — skipping
    the whole citation would let a wrong subcommand hide behind the slot. A
    non-overlay generic mention (``t3 …``, ``t3 <command> …``) is returned
    unchanged.
    """
    toks = raw.split()
    if len(toks) > 1 and (toks[1] == "<overlay>" or toks[1] in _EXAMPLE_OVERLAYS):
        toks[1] = _REPRESENTATIVE_OVERLAY
        return " ".join(toks)
    return raw


def expand_alternations(raw: str) -> list[str]:
    """Every concrete invocation a slash/pipe enumeration in *raw* stands for.

    ``t3 loop enable/disable`` documents two commands, not one path segment
    literally named ``enable/disable``. Only command-word enumerations expand
    (:data:`_ALTERNATION`), so a filesystem path or a JSON arg value is left
    alone. A trailing shell line-continuation is dropped first — it belongs to the
    fenced block's layout, not to the command.
    """
    variants = [""]
    for tok in raw.replace("\\", " ").split():
        alternatives = tok.split("|") if "|" in tok else tok.split("/")
        parts = alternatives if _ALTERNATION.match(tok) else [tok]
        variants = [f"{prefix} {part}".strip() for prefix in variants for part in parts]
    return variants


def _is_placeholder_only(raw: str) -> bool:
    """True for a generic CLI mention whose command path names no concrete command.

    Two shapes qualify. The first token after ``t3`` is a placeholder (``t3 …``,
    ``t3 <command> …``). Or the token AFTER the overlay slot is — a
    ``t3 <overlay> <group> <sub>`` template, whose overlay
    :func:`_resolve_overlay_placeholder` substitutes upstream. An overlay is a
    command GROUP, never a leaf, so substituting it does not by itself produce a
    concrete path: checking only the first token reported that template as drift
    against every registry where the overlay is a group.
    """
    toks = raw.split()[1:]
    if not toks:
        return False
    if _PLACEHOLDER.match(toks[0]) or not _COMMAND_WORD.match(toks[0]):
        return True
    return toks[0] == _REPRESENTATIVE_OVERLAY and len(toks) > 1 and bool(_PLACEHOLDER.match(toks[1]))


def _iter_repo_docs(repo_root: Path) -> Iterable[Path]:
    """Every human-authored doc in the gated corpus, deduplicated and ordered."""
    seen: set[Path] = set()
    for pattern in DOC_GLOBS:
        for md in sorted(repo_root.glob(pattern)):
            rel = md.relative_to(repo_root).as_posix()
            if md in seen or rel.startswith(EXCLUDED_DOC_PREFIXES):
                continue
            seen.add(md)
            yield md


def citation_resolves(raw: str, valid: set[str], groups: set[str]) -> bool | None:
    """Whether *raw* names a live command — ``None`` when it names none at all.

    The single per-citation verdict: the overlay slot is substituted, then every
    alternative a slash/pipe enumeration stands for must resolve. ``None`` is the
    skip verdict for a generic mention whose command path is a placeholder
    (``t3 …``, ``t3 <overlay> <group> <sub>``) or is not a command word at all.
    """
    variants = [_resolve_overlay_placeholder(variant) for variant in expand_alternations(raw)]
    concrete = [variant for variant in variants if not _is_placeholder_only(variant)]
    if not concrete:
        return None
    return all(resolve_command_path(variant, valid, groups) is not None for variant in concrete)


def validate_doc_commands(
    valid: set[str],
    groups: set[str],
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
) -> CommandValidityReport:
    """Validate every backticked ``t3 …`` in the repo's prose docs against the registry.

    *valid* / *groups* are the live CLI registry sets (``command_paths`` /
    ``command_groups`` over the typer app). The corpus is :data:`DOC_GLOBS` — the
    skills tree, the ``agents/*.md`` role briefs, ``BLUEPRINT.md``, and ``docs/``
    minus the generated pages. A backticked invocation that names a concrete
    command which does not resolve is a :class:`CommandViolation` unless
    :data:`ALLOWED_NON_RESOLVING` exempts it. ``checked`` counts the concrete
    invocations examined (placeholder mentions excluded).
    """
    violations: list[CommandViolation] = []
    checked = 0
    for md in _iter_repo_docs(repo_root):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw in iter_backticked_t3_commands(text):
            resolves = citation_resolves(raw, valid, groups)
            if resolves is None:
                continue
            checked += 1
            if not resolves and raw not in ALLOWED_NON_RESOLVING:
                violations.append(CommandViolation(doc=md.relative_to(repo_root).as_posix(), command=raw))
    return CommandValidityReport(violations=tuple(violations), checked=checked)
