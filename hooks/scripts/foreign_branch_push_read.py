"""What a Bash command PUSHES: the reading half of the foreign-branch push gate.

Mechanics only, no policy — the same split as :mod:`foreign_branch_push_shell`
(shell structure) and :mod:`foreign_branch_push_git` (live git). This leaf turns
one command into the pushes it can read, plus the material that hid one; the
gate decides whether each is ours to make.

A push it cannot READ is never silently dropped, save for the shapes named
below. An unresolved wrapper, a quoted script handed to a shell, a push printed
into a payload-less shell, a heredoc a shell executes (whether the shell is the
line's leader or something a wrapper hands the body to), wrappers nested past
the recursion cutoff — each becomes an :class:`Unread` for the gate to refuse,
because a gate that cannot read a command must not conclude "no push here".

THE KNOWN GAPS, open and stated rather than implied. Five shapes still yield
neither a spec nor an unread, so the gate allows them, and all five are
PRE-EXISTING — ``main`` reads none of them either, so none is introduced here.
A shell carrying its own ``-c`` script is read as running THAT rather than
stdin, so a payload which re-executes stdin (``sh -c 'sh'``,
``bash -c 'source /dev/stdin'``) runs a heredoc body unseen. A backtick
substitution and a ``$()`` inside double quotes each stay ONE token — backticks
are absent from ``_PUNCTUATION`` and ``shlex`` does not split a quoted word — so
neither becomes a segment ``names_git_push`` can match; an unquoted ``$(...)``
is judged only incidentally, because ``(`` and ``)`` happen to be separators.
A redirection PRECEDING the command word (``<<'EOF' sh``), and a
backslash-newline continuation between the command word and its ``<<``, both
leave the heredoc's PHYSICAL opener line empty of a consumer, so the executed
body is classified as data.

Whether a body or a payload really pushes is settled by PARSING it, never by
matching its words: over the recorded corpus a text-level scan called every
wrapped ``grep -E 'git push'`` and every ``--get push.default`` a push.

Cold-import safe at module top: stdlib plus the two dependency-free siblings.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from hooks.scripts.foreign_branch_push_argv import PushSpec, git_push_spec
from hooks.scripts.foreign_branch_push_shell import (
    Heredoc,
    command_name,
    flatten_command,
    force_near_push,
    names_git_push,
    names_git_push_in_text,
    past_leaders,
    shell_payload,
)

_SHELL_WRAPPERS: Final[frozenset[str]] = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# Leaders whose operand is NOT a literal script — `eval` re-expands it, `ssh`
# runs it elsewhere, `source` names a file — so parsing it is not parsing what runs.
_SCRIPT_OPERAND_LEADERS: Final[frozenset[str]] = frozenset({"eval", "ssh", "source", "."})
# Builtins that print their operands verbatim rather than running them.
_TEXT_SINKS: Final[frozenset[str]] = frozenset({"echo", "printf"})
# A heredoc reaching one of these is EXECUTED; fed to `cat`, `git commit -F -`,
# `gh` or `python3` it is data, and data is outside this gate's perimeter.
_HEREDOC_EXECUTORS: Final[frozenset[str]] = _SHELL_WRAPPERS | _SCRIPT_OPERAND_LEADERS
_MAX_WRAPPER_DEPTH: Final[int] = 3


@dataclass(frozen=True, slots=True)
class Unread:
    """Material naming a push that this gate could not resolve into a :class:`PushSpec`."""

    kind: str
    leader: str
    force: bool


@dataclass(frozen=True, slots=True)
class ParsedPushes:
    """What a command yielded: the pushes read, and the material left unreadable."""

    specs: tuple[PushSpec, ...]
    unread: tuple[Unread, ...]


@dataclass(slots=True)
class _Collected:
    specs: list[PushSpec] = field(default_factory=list)
    unread: list[Unread] = field(default_factory=list)


def push_specs(command: str, cwd: str) -> ParsedPushes:
    """Every WRITING ``git push`` in *command*, plus the material that hid one.

    A push whose own arguments carry ``--dry-run``/``-n`` writes nothing and is
    omitted. Both fields empty for any command that does not push — the fast
    path that keeps this handler off the forge on every other Bash call.
    """
    collected = _Collected()
    _collect_pushes(command, cwd, collected, depth=0)
    return ParsedPushes(specs=tuple(collected.specs), unread=tuple(collected.unread))


# ── Command parsing ────────────────────────────────────────────────


def _collect_pushes(text: str, cwd: str, into: _Collected, *, depth: int) -> None:
    """Read every push in *text*, and record every shape that hid one."""
    if depth > _MAX_WRAPPER_DEPTH:
        # Nothing past the cutoff is parsed, so a push NAMED here is unread —
        # returning bare made it absent, which is an ALLOW.
        if names_git_push_in_text(text):
            into.unread.append(Unread("nesting", "", force_near_push(text)))
        return
    flat = flatten_command(text)
    work_dir = cwd
    preceding: tuple[str, ...] = ()
    for segment, piped in zip(flat.segments, flat.piped_after, strict=True):
        # `_printed_script` reads the PRECEDING segment as this one's stdin,
        # which is true only across a literal `|` — never across `;`/`&&`/a
        # newline, where the two commands merely run in sequence.
        work_dir = _read_segment(segment, work_dir, into, depth=depth, preceding=preceding if piped else ())
        preceding = segment
    _read_heredocs(flat.heredocs, work_dir, into, depth=depth)
    if flat.unparsable and names_git_push_in_text(text):
        into.unread.append(Unread("unbalanced-quoting", "", force_near_push(text)))


def _read_segment(
    segment: tuple[str, ...], work_dir: str, into: _Collected, *, depth: int, preceding: tuple[str, ...]
) -> str:
    """Read one segment, returning the work dir the following segments run in."""
    leading = past_leaders(segment)
    if not leading:
        return work_dir
    name = command_name(leading[0])
    if name == "cd":
        return _joined(work_dir, leading[1]) if len(leading) > 1 else work_dir
    if name in _SHELL_WRAPPERS and (payload := shell_payload(leading)):
        _collect_pushes(payload, work_dir, into, depth=depth + 1)
    elif name == "git":
        _read_git_push(leading, work_dir, into)
    elif unread := _unread_of(segment, leading, work_dir, depth, preceding):
        into.unread.append(unread)
    return work_dir


def _unread_of(
    segment: tuple[str, ...], leading: list[str], work_dir: str, depth: int, preceding: tuple[str, ...]
) -> Unread | None:
    """The push in *segment* that could not be read, or ``None`` when there is none.

    Each arm scopes its force scan to the text that actually carries the push: a
    force refusal bypasses the shared fail-open chain, so a flag on a neighbour
    must not escalate it.
    """
    name = command_name(leading[0])
    if name in _SHELL_WRAPPERS:
        if names_git_push(segment):
            return Unread("shell-payload", name, force_near_push(" ".join(segment)))
        if _printed_script(preceding, work_dir, depth):
            return Unread("shell-payload", name, force_near_push(" ".join(preceding)))
        return None
    if name in _SCRIPT_OPERAND_LEADERS:
        operand = " ".join(leading[1:])
        return Unread("wrapper", name, force_near_push(operand)) if names_git_push_in_text(operand) else None
    if name not in _TEXT_SINKS and (names_git_push(segment) or _wraps_a_shell_script(segment, work_dir, depth)):
        return Unread("wrapper", command_name(segment[0]), force_near_push(" ".join(segment)))
    return None


def _runs_a_push(script: str, work_dir: str, depth: int) -> bool:
    """Whether *script*, read as shell, really runs a push.

    Reading it beats matching its words: measured over the recorded corpus, a
    text-level scan mistook every wrapped ``grep -E 'git push'`` and every
    ``--get push.default`` for the command it names.
    """
    probe = _Collected()
    _collect_pushes(script, work_dir, probe, depth=depth + 1)
    return bool(probe.specs or probe.unread)


def _wraps_a_shell_script(segment: tuple[str, ...], work_dir: str, depth: int) -> bool:
    """Whether *segment* hands a shell a quoted script that runs a push.

    ``names_git_push`` matches TOKENS, so ``xargs -I{} sh -c 'git push …'`` and
    ``docker exec box bash -lc '… git push …'`` show it one opaque word. The push
    may run in another container or as another user, so it is refused rather than
    read.
    """
    shell = _shell_index(segment)
    return shell is not None and _runs_a_push(shell_payload(segment, shell), work_dir, depth)


def _shell_index(tokens: tuple[str, ...]) -> int | None:
    """Where *tokens* names a shell, so the ``-c`` that is read is the SHELL's own."""
    return next((index for index, word in enumerate(tokens) if command_name(word) in _SHELL_WRAPPERS), None)


def _printed_script(preceding: tuple[str, ...], work_dir: str, depth: int) -> bool:
    """Whether the segment before a payload-less shell PRINTED a push into it.

    ``echo 'git push …' | sh`` runs a push the shell reads from its stdin. A text
    sink is the only producer whose output this gate can read at all; a script
    file or a ``$VAR`` stays outside the perimeter.
    """
    return (
        bool(preceding)
        and command_name(preceding[0]) in _TEXT_SINKS
        and _runs_a_push(" ".join(preceding[1:]), work_dir, depth)
    )


def _read_git_push(leading: list[str], work_dir: str, into: _Collected) -> None:
    if spec := git_push_spec(leading, work_dir):
        into.specs.append(spec)


def _read_heredocs(heredocs: tuple[Heredoc, ...], work_dir: str, into: _Collected, *, depth: int) -> None:
    for heredoc in heredocs:
        executor = _heredoc_executor(heredoc.consumer)
        if executor and _runs_a_push(heredoc.body, work_dir, depth):
            into.unread.append(Unread("shell-heredoc", executor, force_near_push(heredoc.body)))


def _heredoc_executor(consumer: tuple[str, ...]) -> str:
    """What EXECUTES a heredoc handed to *consumer*, or ``""`` when the body is data.

    Resolved THROUGH the wrapper that hands the body on — ``docker exec -i box
    sh`` runs it, the same reach :func:`_wraps_a_shell_script` makes for a ``-c``
    payload; keying on the line's first word reads that as ``docker``, which
    executes nothing.
    """
    for index, word in enumerate(consumer):
        name = command_name(word)
        if name not in _HEREDOC_EXECUTORS:
            continue
        # A shell's own `-c` script displaces stdin as what IT runs, so
        # `bash -c 'cat > f' <<'EOF'` writes the body to a file. Not a general
        # rule: a payload that re-executes stdin (`sh -c 'sh'`) still runs it.
        return "" if name in _SHELL_WRAPPERS and shell_payload(consumer, index) else name
    return ""


def _joined(base: str, target: str) -> str:
    path = Path(target).expanduser()
    return str(path if path.is_absolute() or not base else Path(base) / path)
