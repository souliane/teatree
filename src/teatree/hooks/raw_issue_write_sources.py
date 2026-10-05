"""Static scan: no TRACKED source invokes a raw forge issue write (#162).

:mod:`teatree.core.issue_hygiene` is the single application-level issue-write
facade, and :mod:`teatree.hooks.raw_issue_write_detect` denies the agent's Bash
spelling of the bypass at the PreToolUse boundary. A hook only sees a live
session, though — it cannot see a CI workflow step, a checked-in script, or a
skill that TEACHES the wrong command. Each of those runs outside any session, and
the ci.yml selection-audit filer was exactly that: a bare ``gh issue create`` that
filed one duplicate ticket per PR for years because no gate could reach it.

This module is the other half: the same question asked of the repository's own
tracked text instead of a tool call.

Three things keep it from becoming noise. It asks for an INVOCATION, not a mention
— the traversal is the shared :mod:`teatree.hooks.forge_subcommand` leaf, so a
Python string literal holding an example command and a ``#`` comment are not
writes. A ``FORBIDDEN`` line is a documented counter-example, the repo's existing
spelling for "never do this" in a do-X-never-Y block, and the scan must not punish
a skill for showing the shape it warns against. And a backtick span is read as a
markdown code span rather than a shell command substitution: in tracked prose that
is what it almost always is, and reading it the shell's way made every sentence
*naming* the bypass report itself as one. ``$(...)`` substitution is still
descended into, so a script hiding the write there is still caught.

Wider than the hook on the verbs it can be: ``comment``/``note`` route to
``t3 <overlay> ticket comment --purpose``, and ``create`` to the facade, so a
tracked source invoking either has somewhere to go. ``edit`` and ``close`` are spared for the same
reason the hook spares them — the sweep's own sanctioned fold path IS
``gh issue edit --body-file``, a label-only ``--add-label`` is not a body write at
all, and the facade has no close path yet. Flagging them would report the
sanctioned workflow as the violation.
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks.forge_subcommand import forge_subcommand_argvs

#: Both forges' issue subcommand, so ONE traversal yields the verb as ``argv[0]``.
_ISSUE_SUBWORDS: dict[str, tuple[str, ...]] = {"gh": ("issue",), "glab": ("issue",)}

#: The mutating verbs that HAVE a sanctioned replacement, so a finding is actionable.
#: A read (``view``/``list``) is always allowed; ``edit``/``close`` are deliberately
#: absent (see the module docstring) rather than forgotten.
WRITE_VERBS: frozenset[str] = frozenset(
    {"create", "update", "comment", "note", "reopen", "delete", "lock", "unlock", "transfer"}
)

#: The roots whose tracked contents run outside a session: workflows, executables, skills.
SCANNED_ROOTS: tuple[str, ...] = (".github/", "ci/", "dev/", "hooks/", "scripts/", "skills/", "agents/")

#: Recorded test timings, not a source — a node id names the command it parametrizes.
_EXCLUDED = ("dev/.test_durations",)

#: The repo's existing marker for a do-X-never-Y counter-example.
_DOCUMENTED_MARKER = "FORBIDDEN"

#: A markdown/RST code span, single- or double-backtick. Stripped before lexing so
#: prose naming the bypass is not read as invoking it.
_CODE_SPAN_RE = re.compile(r"`+[^`]+`+")

#: Suffixes whose lines ARE shell. A ``.py`` file is deliberately absent: its commands
#: live in string literals, so shell-lexing one reports its own prose, and a Python
#: caller reaching a forge is already covered by the ``forge-comment-write-seam``
#: chokepoint. What a ``.py`` file CAN hide is an argv list, which :data:`_ARGV_LIST_RE`
#: reads directly — so the two passes together cover both spellings.
_SHELL_SUFFIXES: frozenset[str] = frozenset({"", ".sh", ".bash", ".zsh", ".yml", ".yaml", ".md"})

#: A forge issue write spelled as an argv sequence — ``["gh", "issue", "create", …]``.
#: Scanned in EVERY file type, because this is the shape a Python or JSON caller uses
#: and no shell lexer would ever see it.
_ARGV_LIST_RE = re.compile(r"""['"](?:gh|glab)['"]\s*,\s*['"]issue['"]\s*,\s*['"](\w+)['"]""")


@dataclass(frozen=True, order=True)
class RawIssueWrite:
    """One tracked line that invokes a raw forge issue write."""

    path: str
    line: int
    verb: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: raw `issue {self.verb}` — {self.text.strip()}"


def logical_lines(text: str) -> Iterator[tuple[int, str]]:
    r"""Yield ``(1-based start line, joined text)``, folding ``\\``-continuations into one line.

    A workflow ``run:`` block and a shell script both wrap a long invocation across
    continuations, and half an invocation lexes to nothing — the verb would be on a
    line whose first word is an option.
    """
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not pending:
            start = number
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending.append(stripped[:-1])
            continue
        pending.append(stripped)
        yield start, " ".join(pending)
        pending = []
    if pending:
        yield start, " ".join(pending)


def write_verbs_in(command: str) -> list[str]:
    """The issue write verbs *command* actually EXECUTES, in order; ``[]`` for a mention.

    No error path: the shared lexer is a character state machine with no raise, so a
    half-quoted Python string literal tokenizes to something harmless rather than
    needing a guard here.
    """
    argvs = forge_subcommand_argvs(_CODE_SPAN_RE.sub(" ", command), _ISSUE_SUBWORDS)
    return [argv[0] for argv in argvs if argv and argv[0] in WRITE_VERBS]


def argv_list_verbs_in(line: str) -> list[str]:
    """The issue write verbs *line* spells as an argv sequence, in order."""
    return [verb for verb in _ARGV_LIST_RE.findall(line) if verb in WRITE_VERBS]


def scan_text(text: str, *, path: str) -> list[RawIssueWrite]:
    """Every raw issue write *text* invokes, skipping documented counter-examples.

    Shell-lexed only for a shell-bearing suffix; the argv-list shape is read in every
    file, so a Python or JSON caller cannot hide behind the suffix split.
    """
    shell = Path(path).suffix.casefold() in _SHELL_SUFFIXES
    found: list[RawIssueWrite] = []
    for number, line in logical_lines(text):
        if _DOCUMENTED_MARKER in line:
            continue
        verbs = [*(write_verbs_in(line) if shell else []), *argv_list_verbs_in(line)]
        found.extend(RawIssueWrite(path=path, line=number, verb=verb, text=line) for verb in verbs)
    return found


def in_scope(path: str) -> bool:
    """Whether *path* is a scanned workflow / executable / skill source."""
    return path.startswith(SCANNED_ROOTS) and not path.startswith(_EXCLUDED)


def scan_paths(paths: Iterable[str], *, root: Path) -> list[RawIssueWrite]:
    """Scan the in-scope members of *paths*, resolved under *root*."""
    found: list[RawIssueWrite] = []
    for path in sorted(p for p in paths if in_scope(p)):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # a binary or vanished tracked file holds no command
            continue
        found.extend(scan_text(text, path=path))
    return found


__all__ = [
    "SCANNED_ROOTS",
    "WRITE_VERBS",
    "RawIssueWrite",
    "in_scope",
    "logical_lines",
    "scan_paths",
    "scan_text",
    "write_verbs_in",
]
