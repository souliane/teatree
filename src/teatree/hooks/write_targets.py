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
it cannot pin statically (a ``$VAR`` path, a ``>(...)`` process substitution, an
interpreter body that writes through a variable) is reported as
:attr:`WriteTargets.unresolved` rather than guessed at — each consumer then
applies its own posture (the main-clone guard ALLOWS an unresolvable target,
matching its existing stance on an unpinnable git target; the plan gate warns
rather than denying).

Segmentation runs through the shared quote-accurate :mod:`_shell_lexer`, so a
write verb inside a quoted string or a heredoc body is never mistaken for a
command, and heredoc bodies are attributed to the command that opened them.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from teatree.hooks._shell_lexer import Token, TokenKind, split_commands, tokenize

_MAX_INTERPRETER_DEPTH: Final[int] = 3

# A heredoc and its body. The delimiter is any shell WORD, not just ``\w+``: bash
# takes ``<<'PY-1'`` / ``<<"body.txt"`` exactly as it takes ``<<EOF``, and a
# ``\w+``-only delimiter left the whole body unpaired — so an interpreter heredoc
# spelled that way reported no write targets AND no unresolved target, which the
# write gates read as "this command writes nothing". Quote characters and
# whitespace stay out of the class so the delimiter cannot swallow the rest of
# the line; the terminator is the backreference, matched literally.
_HEREDOC_RE: Final[re.Pattern[str]] = re.compile(
    r"<<-?\s*['\"]?(?P<delim>[A-Za-z0-9_][\w.+:@%^=,~-]*)['\"]?[^\n]*\n(?P<body>.*?)\n[ \t]*(?P=delim)[ \t]*(?=\n|$)",
    re.DOTALL,
)

_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# An output redirect, optionally fd-qualified: ``>`` / ``>>`` / ``>|`` / ``2>``.
_REDIRECT_RE: Final[re.Pattern[str]] = re.compile(r"^\d*(?:>>|>\|?)")
# A target the shell expands at run time — the hook cannot pin it, so it is
# reported unresolved instead of matched as the literal pre-expansion text. The
# parens cover process substitution: ``_REDIRECT_RE`` matches the leading ``>``
# of ``>(...)``, so its split-token remainder (``(gzip``, ``/tmp/a.gz)``) would
# otherwise be emitted as a literal path that does not exist (#4127). Bash
# expands the substitution to a ``/dev/fd/N``, so unresolved is the honest answer.
_SUBSTITUTION_CHARS: Final[frozenset[str]] = frozenset({"$", "`", "*", "?", "(", ")"})

_SED_NAMES: Final[frozenset[str]] = frozenset({"sed", "gsed"})
_PERL_NAMES: Final[frozenset[str]] = frozenset({"perl"})
_COPY_NAMES: Final[frozenset[str]] = frozenset({"cp", "mv", "install"})
_SHELL_NAMES: Final[frozenset[str]] = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# Prefixes that run the NEXT word rather than being the command themselves.
# ``command cp`` / ``command mv`` is the alias-safe spelling the house shell
# rules mandate, so reading the wrapper as the leader would turn the mandated
# spelling into a blanket bypass of both write gates.
_WRAPPER_LEADERS: Final[frozenset[str]] = frozenset({"command", "env", "nohup", "time", "stdbuf", "nice"})
_SED_SCRIPT_FLAGS: Final[frozenset[str]] = frozenset({"-e", "--expression", "-f", "--file"})
_COPY_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group"}
)
# git's leading global options that consume the NEXT token as their value, so
# the subcommand scanner skips two tokens for them (``git -C <path> mv``).
_GIT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
_IN_PLACE_CHARS: Final[frozenset[str]] = frozenset("i")


@dataclass(frozen=True, slots=True)
class _InPlaceEditor:
    """One in-place editor's flag alphabet, for the shared bundled-cluster walk.

    ``arg_chars`` take an argument, so the cluster's remainder is that value and
    the walk ends there — ``sed -ei 's/a/b/'`` runs the script ``i`` rather than
    editing in place. ``next_arg_chars`` REQUIRE that argument, so a cluster
    ending on one takes the FOLLOWING word; ``-i``'s suffix is optional, which is
    why it belongs to the first set and not the second.
    """

    arg_chars: frozenset[str]
    next_arg_chars: frozenset[str]
    script_chars: frozenset[str]
    long_in_place: tuple[str, ...] = ()
    long_script_flags: frozenset[str] = frozenset()
    long_script_prefixes: tuple[str, ...] = ()


_SED: Final[_InPlaceEditor] = _InPlaceEditor(
    arg_chars=frozenset("efil"),
    next_arg_chars=frozenset("efl"),
    script_chars=frozenset("ef"),
    long_in_place=("--in-place",),
    long_script_flags=_SED_SCRIPT_FLAGS,
    long_script_prefixes=("--expression=", "--file="),
)
# perl spells every one of these short-only — it has no long form for -i or -e.
_PERL: Final[_InPlaceEditor] = _InPlaceEditor(
    arg_chars=frozenset("0CDFIVdeilmMxE"),
    next_arg_chars=frozenset("eEFI"),
    script_chars=frozenset("eE"),
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
        words = [token for token in segment if token.kind is TokenKind.WORD]
        found, missed = _segment_write_targets(words, heredocs, depth=depth)
        targets.extend(found)
        unresolved = unresolved or missed
    return WriteTargets(targets=tuple(dict.fromkeys(targets)), unresolved=unresolved)


def _heredoc_bodies(command: str) -> dict[str, str]:
    """Map every heredoc delimiter in ``command`` to its body text."""
    return {match.group("delim"): match.group("body") for match in _HEREDOC_RE.finditer(command)}


def _segment_write_targets(words: list[Token], heredocs: dict[str, str], *, depth: int) -> tuple[list[str], bool]:
    leader, operands = _leader_and_operands(words)
    if not leader:
        return [], False
    raw = _redirect_raw_targets(operands)
    if leader in _SED_NAMES:
        raw.extend(_in_place_raw_targets(operands, _SED))
    elif leader in _PERL_NAMES:
        raw.extend(_in_place_raw_targets(operands, _PERL))
    elif leader == "tee":
        raw.extend(_plain_positionals(operands[1:]))
    elif leader in _COPY_NAMES:
        raw.extend(_copy_raw_destination(operands))
    elif leader == "git":
        raw.extend(_git_mv_raw_destination(operands))
    targets, unresolved = _classify(raw)
    body_targets, body_unresolved = _interpreter_body_targets(leader, operands, heredocs, depth=depth)
    return targets + body_targets, unresolved or body_unresolved


def _leader_and_operands(words: list[Token]) -> tuple[str, list[Token]]:
    """The command's basename leader plus its words, past prefixes that do not run it.

    An env assignment is recognised on the VERBATIM span, per the shell's own
    rule: ``'A'=1`` decodes to ``A=1`` but bash runs a command literally NAMED
    ``A=1``, so a quoted spelling is not an assignment. The wrapper prefixes are
    skipped because ``command cp`` / ``command mv`` is the alias-safe spelling
    the house shell rules mandate — reading the wrapper as the leader would make
    the mandated spelling a blanket bypass.
    """
    index = 0
    while index < len(words) and (
        _ENV_ASSIGNMENT_RE.match(words[index].raw) or PurePosixPath(words[index].value).name in _WRAPPER_LEADERS
    ):
        index += 1
    if index >= len(words):
        return "", []
    return PurePosixPath(words[index].value).name, words[index:]


def _classify(raw_targets: list[str]) -> tuple[list[str], bool]:
    """Split raw targets into statically-pinned paths and an unresolved flag."""
    targets = [t for t in raw_targets if t and not (_SUBSTITUTION_CHARS & set(t))]
    return targets, len(targets) != len(raw_targets)


def _redirect_is_live(word: Token) -> "re.Match[str] | None":
    """The redirect operator at the head of ``word``, or None if it is not one.

    Matched on the VERBATIM span: redirection is shell SYNTAX, so a ``>`` the
    shell never sees as an operator — ``grep -rn '>' src/``, a markdown
    blockquote in ``-m "> note"`` — is an ordinary argument. Matching the decoded
    value instead read those as "writes src/" and denied everyday commands.
    """
    return _REDIRECT_RE.match(word.raw)


def _redirect_raw_targets(words: list[Token]) -> list[str]:
    """Targets of every output redirect in the segment (fd duplication excluded)."""
    raw: list[str] = []
    for index, word in enumerate(words):
        match = _redirect_is_live(word)
        if match is None:
            continue
        suffix = word.value[match.end() :]
        target = suffix or (words[index + 1].value if index + 1 < len(words) else "")
        if target and not target.startswith("&"):
            raw.append(target)
    return raw


def _in_place_raw_targets(words: list[Token], editor: _InPlaceEditor) -> list[str]:
    """The files an in-place editor rewrites; empty when it is not editing in place.

    With no script flag the FIRST positional is the script itself (``sed 's/a/b/'
    f``) or the program file (``perl rewrite.pl f``), never a file being written.
    """
    flags = [word.value for word in words[1:]]
    if not any(_names_in_place(flag, editor) for flag in flags):
        return []
    positionals = _plain_positionals(words[1:], value_flags=editor.long_script_flags, editor=editor)
    if any(_names_script(flag, editor) for flag in flags):
        return positionals
    return positionals[1:]


def _names_in_place(flag: str, editor: _InPlaceEditor) -> bool:
    return _cluster_names(flag, _IN_PLACE_CHARS, editor) or flag.startswith(editor.long_in_place)


def _names_script(flag: str, editor: _InPlaceEditor) -> bool:
    return (
        _cluster_names(flag, editor.script_chars, editor)
        or flag in editor.long_script_flags
        or flag.startswith(editor.long_script_prefixes)
    )


def _cluster_names(flag: str, wanted: frozenset[str], editor: _InPlaceEditor) -> bool:
    """True iff a bundled short-option cluster names an option in ``wanted``.

    getopt reads a cluster left to right, so ``-ni`` / ``-Ei`` name ``-i`` exactly
    as ``-i`` does; keying on ``startswith("-i")`` missed every bundled spelling
    and reported the command as writing nothing at all.
    """
    if not flag.startswith("-") or flag.startswith("--"):
        return False
    for char in flag[1:]:
        if char in wanted:
            return True
        if char in editor.arg_chars:
            return False
    return False


def _cluster_takes_next_word(flag: str, editor: _InPlaceEditor) -> bool:
    """True iff the cluster ends on an option whose REQUIRED argument is the next word."""
    if not flag.startswith("-") or flag.startswith("--"):
        return False
    for index, char in enumerate(flag[1:], start=2):
        if char in editor.arg_chars:
            return index == len(flag) and char in editor.next_arg_chars
    return False


def _copy_raw_destination(words: list[Token]) -> list[str]:
    """The destination of a ``cp``/``mv``/``install`` — the ``-t`` dir or the last operand."""
    for index, word in enumerate(words):
        if word.value in {"-t", "--target-directory"} and index + 1 < len(words):
            return [words[index + 1].value]
        if word.value.startswith("--target-directory="):
            return [word.value.split("=", 1)[1]]
    positionals = _plain_positionals(words[1:], value_flags=_COPY_VALUE_FLAGS)
    return positionals[-1:] if len(positionals) > 1 else []


def _git_mv_raw_destination(words: list[Token]) -> list[str]:
    """The destination of a ``git mv``, skipping git's leading global options."""
    cursor = 1
    while cursor < len(words):
        if not words[cursor].value.startswith("-"):
            break
        cursor += 2 if words[cursor].value in _GIT_VALUE_FLAGS else 1
    else:
        return []
    if words[cursor].value != "mv":
        return []
    positionals = _plain_positionals(words[cursor + 1 :])
    return positionals[-1:] if len(positionals) > 1 else []


def _plain_positionals(
    words: list[Token], value_flags: frozenset[str] = _NO_VALUE_FLAGS, editor: _InPlaceEditor | None = None
) -> list[str]:
    """Positional operands only — flags, their values, and redirects dropped.

    Redirects and input redirections are recognised on the verbatim span (shell
    syntax); flags are recognised on the decoded value, because quoting does not
    change how the PROGRAM parses its options (``sed '-i'`` is still ``-i``). An
    empty operand (BSD ``sed -i ''``) is dropped so it cannot shift the slice
    that picks a script or a destination out of the positionals.
    """
    positionals: list[str] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        match = _redirect_is_live(word)
        if match is not None:
            skip_next = not word.value[match.end() :]
            continue
        if word.raw.startswith("<"):
            skip_next = word.value in {"<", "<<", "<<-"}
            continue
        if word.value == "--":
            continue
        if word.value.startswith("-") and word.value != "-":
            skip_next = word.value in value_flags or (
                editor is not None and _cluster_takes_next_word(word.value, editor)
            )
            continue
        if word.value:
            positionals.append(word.value)
    return positionals


def _interpreter_body_targets(
    leader: str, words: list[Token], heredocs: dict[str, str], *, depth: int
) -> tuple[list[str], bool]:
    """Write targets named inside an interpreter's heredoc / ``-c`` body."""
    is_shell = leader in _SHELL_NAMES
    is_python = leader.startswith("python")
    if depth >= _MAX_INTERPRETER_DEPTH or not (is_shell or is_python):
        return [], False
    bodies, unread_heredoc = _interpreter_bodies(words, heredocs)
    targets: list[str] = []
    unresolved = unread_heredoc
    for body in bodies:
        if is_shell:
            nested = _command_write_targets(body, depth=depth + 1)
            targets.extend(nested.targets)
            unresolved = unresolved or nested.unresolved
        else:
            found, missed = _python_body_targets(body)
            targets.extend(found)
            unresolved = unresolved or missed
    return targets, unresolved


def _interpreter_bodies(words: list[Token], heredocs: dict[str, str]) -> tuple[list[str], bool]:
    """The code an interpreter segment runs, and whether a heredoc body went UNREAD.

    The bodies are the segment's heredocs plus any ``-c`` value. The heredoc
    operator is recognised on the verbatim span (shell syntax); the delimiter
    comes from the decoded value, so ``<<'PY'`` and ``<<PY`` name the same body.

    A heredoc whose delimiter :func:`_heredoc_bodies` could not pair with a body
    is reported UNREAD, not dropped: the segment demonstrably feeds an interpreter
    a script this module cannot see, which is the module's ``unresolved`` answer —
    silently returning no bodies made it indistinguishable from a segment that
    writes nothing, and both write gates key on that distinction.
    """
    bodies: list[str] = []
    unread = False
    expect_delimiter = False
    for index, word in enumerate(words):
        delimiter: str | None = None
        if expect_delimiter:
            expect_delimiter = False
            delimiter = word.value
        elif not word.raw.startswith("<<"):
            if word.value == "-c" and index + 1 < len(words):
                bodies.append(words[index + 1].value)
        elif word.value in {"<<", "<<-"}:
            expect_delimiter = True
        else:
            delimiter = word.value.removeprefix("<<-").removeprefix("<<")
        if delimiter is None:
            continue
        body = heredocs.get(delimiter.strip("'\""))
        if body is None:
            unread = True
        else:
            bodies.append(body)
    return bodies, unread


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
    writes = bool(_PY_UNPINNED_OPEN_RE.search(body)) or bool(_PY_WRITE_EVIDENCE_RE.search(body))
    return pinned, unresolved or (writes and not pinned)
