r"""The filesystem paths a Bash command would WRITE (#4091/#4092).

The plan-before-code gate and the main-clone guard both keyed on the
``Edit``/``Write`` TOOL NAMES, so a file written through the shell — a
``python3 - <<PY`` heredoc, ``sed -i``, ``cat > path``, ``cp`` — reached
neither gate. Measured cost: a full day of implementation during which the
plan gate never fired once, and a deployed main clone edited by cwd drift with
nothing firing at write time.

This module is the shared answer both gates consume. It is deliberately
PRECISION-biased: a false negative is exactly today's behaviour and costs
nothing new, while a false positive blocks legitimate shell work. So a target
it cannot pin statically (a ``$VAR`` path, an interpreter body that writes
through a variable) is reported as :attr:`WriteTargets.unresolved` rather than
guessed at — each consumer then applies its own posture (the main-clone guard
ALLOWS an unresolvable target, matching its existing stance on an unpinnable
git target; the plan gate warns rather than denying).

Segmentation runs through the shared quote-accurate :mod:`_shell_lexer`, so a
write verb inside a quoted string or a heredoc body is never mistaken for a
command, and heredoc bodies are attributed to the command that opened them.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize

_MAX_INTERPRETER_DEPTH: Final[int] = 3

_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# An output redirect, optionally fd-qualified: ``>`` / ``>>`` / ``>|`` / ``2>``.
_REDIRECT_RE: Final[re.Pattern[str]] = re.compile(r"^\d*(?:>>|>\|?)")
# A target the shell expands at run time — the hook cannot pin it, so it is
# reported unresolved instead of matched as the literal pre-expansion text.
_SUBSTITUTION_CHARS: Final[frozenset[str]] = frozenset({"$", "`", "*", "?"})

_SED_NAMES: Final[frozenset[str]] = frozenset({"sed", "gsed"})
_COPY_NAMES: Final[frozenset[str]] = frozenset({"cp", "mv", "install"})
_SHELL_NAMES: Final[frozenset[str]] = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_SED_SCRIPT_FLAGS: Final[frozenset[str]] = frozenset({"-e", "--expression", "-f", "--file"})
_COPY_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group"}
)
# git's leading global options that consume the NEXT token as their value, so
# the subcommand scanner skips two tokens for them (``git -C <path> mv``).
_GIT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)

# A literal path opened in a WRITE mode inside an interpreter body.
_PY_OPEN_RE: Final[re.Pattern[str]] = re.compile(
    r"""open\(\s*(?P<q>['"])(?P<path>[^'"]+)(?P=q)\s*,\s*(?P<mq>['"])(?P<mode>[^'"]*)(?P=mq)"""
)
_PY_PATH_WRITE_RE: Final[re.Pattern[str]] = re.compile(
    r"""Path\(\s*(?P<q>['"])(?P<path>[^'"]+)(?P=q)\s*\)\s*\.write_(?:text|bytes)\("""
)
# Evidence the body writes SOMETHING even when no literal path could be pinned:
# a write-mode ``open`` whose path is a variable, or a path-free write API.
_PY_UNPINNED_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"""open\([^)]*,\s*['"][^'"]*[wax][^'"]*['"]""")
_PY_WRITE_EVIDENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"\.write_text\(|\.write_bytes\(|\.writelines\(|shutil\.(?:copy|move)|os\.(?:replace|rename)\("
)
_PY_WRITE_MODE_CHARS: Final[frozenset[str]] = frozenset({"w", "a", "x", "+"})
_NO_VALUE_FLAGS: Final[frozenset[str]] = frozenset()


@dataclass(frozen=True, slots=True)
class WriteTargets:
    """The write targets a command names, plus whether one could not be pinned.

    ``targets`` are the paths exactly as written in the command (relative paths
    stay relative — :meth:`resolved_paths` anchors them). ``unresolved`` is True
    when the command clearly writes but the destination is not statically
    knowable, which is an AMBIGUOUS signal, never a proof of a write.
    """

    targets: tuple[str, ...]
    unresolved: bool

    @property
    def writes_something(self) -> bool:
        return bool(self.targets) or self.unresolved

    def resolved_paths(self, base: Path | None) -> tuple[Path, ...]:
        """Absolute paths for every target, anchoring relatives to ``base``.

        A relative target with no ``base`` to anchor it is DROPPED rather than
        resolved against the hook subprocess's own cwd, which has usually reset
        away from the directory the command runs in.
        """
        resolved: list[Path] = []
        for target in self.targets:
            path = Path(target).expanduser()
            if path.is_absolute():
                resolved.append(path)
            elif base is not None:
                resolved.append(base / path)
        return tuple(resolved)


def bash_write_targets(command: str) -> WriteTargets:
    """Return the paths ``command`` would write, per the precision-biased rules."""
    return _command_write_targets(command, depth=0)


def _command_write_targets(command: str, *, depth: int) -> WriteTargets:
    heredocs = _heredoc_bodies(command)
    targets: list[str] = []
    unresolved = False
    for segment in split_commands(tokenize(command)):
        words = [token.value for token in segment if token.kind is TokenKind.WORD]
        found, missed = _segment_write_targets(words, heredocs, depth=depth)
        targets.extend(found)
        unresolved = unresolved or missed
    return WriteTargets(targets=tuple(dict.fromkeys(targets)), unresolved=unresolved)


def _heredoc_bodies(command: str) -> dict[str, str]:
    """Map every heredoc delimiter in ``command`` to its body text."""
    pattern = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n[ \t]*\1[ \t]*(?=\n|$)", re.DOTALL)
    return {match.group(1): match.group(2) for match in pattern.finditer(command)}


def _segment_write_targets(words: list[str], heredocs: dict[str, str], *, depth: int) -> tuple[list[str], bool]:
    leader, operands = _leader_and_operands(words)
    if not leader:
        return [], False
    raw = _redirect_raw_targets(operands)
    if leader in _SED_NAMES:
        raw.extend(_sed_raw_targets(operands))
    elif leader == "tee":
        raw.extend(_plain_positionals(operands[1:]))
    elif leader in _COPY_NAMES:
        raw.extend(_copy_raw_destination(operands))
    elif leader == "git":
        raw.extend(_git_mv_raw_destination(operands))
    targets, unresolved = _classify(raw)
    body_targets, body_unresolved = _interpreter_body_targets(leader, operands, heredocs, depth=depth)
    return targets + body_targets, unresolved or body_unresolved


def _leader_and_operands(words: list[str]) -> tuple[str, list[str]]:
    """The command's basename leader plus its words, past any env assignments."""
    index = 0
    while index < len(words) and _ENV_ASSIGNMENT_RE.match(words[index]):
        index += 1
    if index >= len(words):
        return "", []
    return PurePosixPath(words[index]).name, words[index:]


def _classify(raw_targets: list[str]) -> tuple[list[str], bool]:
    """Split raw targets into statically-pinned paths and an unresolved flag."""
    targets = [t for t in raw_targets if t and not (_SUBSTITUTION_CHARS & set(t))]
    return targets, len(targets) != len(raw_targets)


def _redirect_raw_targets(words: list[str]) -> list[str]:
    """Targets of every output redirect in the segment (fd duplication excluded)."""
    raw: list[str] = []
    for index, word in enumerate(words):
        match = _REDIRECT_RE.match(word)
        if match is None:
            continue
        suffix = word[match.end() :]
        target = suffix or (words[index + 1] if index + 1 < len(words) else "")
        if target and not target.startswith("&"):
            raw.append(target)
    return raw


def _sed_raw_targets(words: list[str]) -> list[str]:
    """The files a ``sed -i`` rewrites in place; empty for a read-only sed."""
    flags = words[1:]
    if not any(_is_sed_in_place(flag) for flag in flags):
        return []
    positionals = _plain_positionals(flags, value_flags=_SED_SCRIPT_FLAGS)
    if any(flag in _SED_SCRIPT_FLAGS or flag.startswith(("--expression=", "--file=")) for flag in flags):
        return positionals
    return positionals[1:]


def _is_sed_in_place(flag: str) -> bool:
    return flag.startswith("--in-place") or (flag.startswith("-i") and not flag.startswith("--"))


def _copy_raw_destination(words: list[str]) -> list[str]:
    """The destination of a ``cp``/``mv``/``install`` — the ``-t`` dir or the last operand."""
    for index, word in enumerate(words):
        if word in {"-t", "--target-directory"} and index + 1 < len(words):
            return [words[index + 1]]
        if word.startswith("--target-directory="):
            return [word.split("=", 1)[1]]
    positionals = _plain_positionals(words[1:], value_flags=_COPY_VALUE_FLAGS)
    return positionals[-1:] if len(positionals) > 1 else []


def _git_mv_raw_destination(words: list[str]) -> list[str]:
    """The destination of a ``git mv``, skipping git's leading global options."""
    cursor = 1
    while cursor < len(words):
        token = words[cursor]
        if not token.startswith("-"):
            break
        cursor += 2 if token in _GIT_VALUE_FLAGS else 1
    else:
        return []
    if words[cursor] != "mv":
        return []
    positionals = _plain_positionals(words[cursor + 1 :])
    return positionals[-1:] if len(positionals) > 1 else []


def _plain_positionals(words: list[str], value_flags: frozenset[str] = _NO_VALUE_FLAGS) -> list[str]:
    """Positional operands only — flags, their values, and redirects dropped."""
    positionals: list[str] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        match = _REDIRECT_RE.match(word)
        if match is not None:
            skip_next = not word[match.end() :]
            continue
        if word.startswith("<"):
            skip_next = word in {"<", "<<", "<<-"}
            continue
        if word == "--":
            continue
        if word.startswith("-") and word != "-":
            skip_next = word in value_flags
            continue
        positionals.append(word)
    return positionals


def _interpreter_body_targets(
    leader: str, words: list[str], heredocs: dict[str, str], *, depth: int
) -> tuple[list[str], bool]:
    """Write targets named inside an interpreter's heredoc / ``-c`` body."""
    is_shell = leader in _SHELL_NAMES
    is_python = leader.startswith("python")
    if depth >= _MAX_INTERPRETER_DEPTH or not (is_shell or is_python):
        return [], False
    targets: list[str] = []
    unresolved = False
    for body in _interpreter_bodies(words, heredocs):
        if is_shell:
            nested = _command_write_targets(body, depth=depth + 1)
            targets.extend(nested.targets)
            unresolved = unresolved or nested.unresolved
        else:
            found, missed = _python_body_targets(body)
            targets.extend(found)
            unresolved = unresolved or missed
    return targets, unresolved


def _interpreter_bodies(words: list[str], heredocs: dict[str, str]) -> list[str]:
    """The code an interpreter segment runs: its heredoc bodies plus any ``-c`` value."""
    bodies: list[str] = []
    expect_delimiter = False
    for index, word in enumerate(words):
        if expect_delimiter:
            expect_delimiter = False
            bodies.extend(_body_for_delimiter(word, heredocs))
            continue
        if word in {"<<", "<<-"}:
            expect_delimiter = True
        elif word.startswith("<<"):
            bodies.extend(_body_for_delimiter(word.removeprefix("<<-").removeprefix("<<"), heredocs))
        elif word == "-c" and index + 1 < len(words):
            bodies.append(words[index + 1])
    return bodies


def _body_for_delimiter(delimiter: str, heredocs: dict[str, str]) -> list[str]:
    body = heredocs.get(delimiter.strip("'\""))
    return [body] if body is not None else []


def _python_body_targets(body: str) -> tuple[list[str], bool]:
    """Literal write paths in a Python body, and whether it writes unpinnably.

    ``unresolved`` is raised only when the body carries POSITIVE evidence of a
    write (a write-mode ``open``, a ``write_text``/``write_bytes``, a
    ``shutil.copy``/``move``, an ``os.replace``/``rename``) whose destination is
    not a literal — a read-only probe body is not a write and must not warn.
    """
    targets = [match.group("path") for match in _PY_PATH_WRITE_RE.finditer(body)]
    write_opens = [match for match in _PY_OPEN_RE.finditer(body) if _PY_WRITE_MODE_CHARS & set(match.group("mode"))]
    targets.extend(match.group("path") for match in write_opens)
    pinned, unresolved = _classify(targets)
    if pinned:
        return pinned, unresolved
    writes = bool(_PY_UNPINNED_OPEN_RE.search(body)) or bool(_PY_WRITE_EVIDENCE_RE.search(body))
    return [], unresolved or writes
