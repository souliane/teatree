"""Detect an UNBOUNDED agent-authored wait loop — the PreToolUse bounded-wait gate (#3882).

Pure command analysis (no ORM, no ``ps``, no process state) so the PreToolUse hook
stays fast and the detection is unit-testable. The gate this drives refuses to
CREATE an unbounded wait; it never looks for one that is already running and never
signals a process, so no part of it can act on an inferred-dead signal.

The shape it refuses. An agent blocks on CI, a commit, or a background job by
writing ``until <condition>; do sleep N; done``. The loop is a child of the
session's shell, so when that session ends — crash, token exhaustion, harness
restart, user stop — the loop is reparented to init and keeps polling. Its exit
condition may never become true, because the thing it waited for was resolved by
someone else or abandoned entirely, and with no deadline "never" is literal: the
loop runs until the box is rebooted, spending a subprocess per tick forever.

An ``until``/``while`` on an EXTERNAL condition is therefore never correct without
a bound — a condition can stop being reachable. What the gate requires is that the
wait ends for a reason of its own, in any of the forms a shell offers:

* a ``timeout`` wrapper (``timeout 1800 bash -c '…'``) — the whole wait is killed at
    the deadline and exits non-zero, which the agent sees;
* the bash ``SECONDS`` elapsed builtin in the loop condition;
* an epoch deadline computed with ``date +%s``;
* the loop's lifetime tied to its own session, by testing ``$PPID`` — the spawning
    shell — so the wait ends when the session that wanted the answer is gone. The
    loop exits ITSELF on a positive ``kill -0`` probe of its own parent; nothing is
    signalled and no third party's liveness is judged.

Deliberately NOT flagged. A bare ``sleep N`` (one nap, bounded by construction). A
``for`` loop (a finite iteration list). A ``while read`` loop (bounded by its
input). A loop keyword inside a quoted string or a comment — segmentation is
anchored at a command position through the shared quote-accurate shell lexer, so
``echo "until X; do sleep 5; done"`` is prose, not a loop.
"""

from dataclasses import dataclass

from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize

#: Loop keywords whose iteration count is decided by an EXTERNAL condition, so
#: nothing about the loop itself bounds it. ``for`` is absent on purpose: its
#: iteration list is finite at the point the loop starts.
_UNBOUNDED_LOOP_KEYWORDS = frozenset({"until", "while"})

#: The command that makes a wait bounded — a hard deadline after which the wait is
#: killed and exits non-zero. ``gtimeout`` is the same tool under coreutils on macOS.
_TIMEOUT_COMMANDS = frozenset({"timeout", "gtimeout"})

#: Command words that RUN a shell command supplied as a string argument. The wait
#: may be written inside one of these (``bash -c '…'``, ``eval '…'``), so their
#: string arguments are re-analysed one nesting level down. Restricted to genuine
#: shell runners so a loop quoted as DATA (``echo "…"``) is never re-parsed as code.
_SHELL_RUNNERS = frozenset({"bash", "sh", "zsh", "dash", "eval"})

#: Words that precede the real command word without being it — shell block/branch
#: keywords and exec-style prefixes. Stripped so the head of ``do sleep 5`` is
#: ``sleep`` and the head of ``nohup bash -c '…'`` is ``bash``.
_LEADING_NOISE = frozenset({"do", "then", "else", "{", "(", "!", "nohup", "setsid", "command", "exec", "time"})

#: Elapsed-time sources an in-shell deadline is built from — the bash ``SECONDS``
#: builtin and a ``date +%s`` epoch stamp (in any quoting). Their presence anywhere
#: in the command is read as "this wait computes its own deadline"; the condition
#: that uses them is the author's, and this module does not evaluate it.
_DEADLINE_MARKERS = ("SECONDS", "+%s")

#: The spawning shell's pid. A wait that tests it is bound to the LIFETIME of the
#: session that started it — the second bound the defect asks for — so it ends when
#: nobody is left to read the answer. Deliberately narrow: a bare ``kill -0 <other
#: pid>`` is NOT a bound (``while kill -0 "$job"`` waits as long as that job hangs),
#: whereas ``$PPID`` names exactly one process, this wait's own parent.
_SESSION_LIFETIME_MARKER = "PPID"

#: Any of the above. A wait carrying one of these ends for a reason of its own.
_BOUND_MARKERS = (*_DEADLINE_MARKERS, _SESSION_LIFETIME_MARKER)

#: How far to follow ``bash -c`` / ``eval`` nesting. Two levels covers every shape
#: seen in practice and keeps a pathological command from driving deep recursion.
_MAX_NESTING = 2

_BLOCK_MSG = (
    "BLOCKED: this command starts an UNBOUNDED wait loop — an `until`/`while` whose body "
    "sleeps, with no deadline. A background wait outlives the session that spawned it: when "
    "the session ends the loop is reparented and keeps polling, and its condition may never "
    "become true because the thing it waited for was resolved by someone else or abandoned. "
    "Give the wait a deadline so it ends on its own and says so:\n"
    "  timeout 1800 bash -c 'until <condition>; do sleep 20; done' "
    '|| echo "WAIT TIMED OUT after 1800s"\n'
    "or bound it in-shell with the bash elapsed-seconds builtin:\n"
    '  SECONDS=0; until <condition> || [ "$SECONDS" -ge 1800 ]; do sleep 20; done\n'
    "or tie the wait to the lifetime of the session that wants the answer:\n"
    '  until <condition> || ! kill -0 "$PPID" 2>/dev/null; do sleep 20; done\n'
    "Each form ends for a reason of its own and reports it, instead of polling forever. "
    "Pick a bound you would actually wait for, then re-check rather than block longer."
)


@dataclass(frozen=True, slots=True)
class UnboundedWaitDetection:
    """Whether a Bash command starts a wait loop that carries no deadline."""

    is_unbounded_wait: bool
    message: str


@dataclass(frozen=True, slots=True)
class _WaitShape:
    """What one command (at one nesting level) contains."""

    has_loop: bool
    has_sleep: bool
    has_bound: bool

    def merged_with(self, other: "_WaitShape") -> "_WaitShape":
        return _WaitShape(
            has_loop=self.has_loop or other.has_loop,
            has_sleep=self.has_sleep or other.has_sleep,
            has_bound=self.has_bound or other.has_bound,
        )


def _strip_leading_noise(words: list[str]) -> tuple[list[str], bool]:
    """Split *words* into (real command words, whether a ``timeout`` wrapper was consumed).

    Block keywords, exec-style prefixes, and a ``timeout <duration>`` wrapper all
    precede the segment's real command word, so ``do sleep 5`` reduces to
    ``sleep 5`` and ``timeout 30m bash -c '…'`` reduces to ``bash -c '…'`` while
    reporting the deadline the wrapper supplies.
    """
    index = 0
    timed = False
    while index < len(words):
        word = words[index]
        if word in _LEADING_NOISE:
            index += 1
            continue
        if word in _TIMEOUT_COMMANDS:
            timed = True
            index += 1
            # Consume the duration operand and any flags between it and the command.
            while index < len(words) and (words[index].startswith("-") or _looks_like_duration(words[index])):
                index += 1
            continue
        break
    return words[index:], timed


def _looks_like_duration(word: str) -> bool:
    """Whether *word* is a ``timeout`` duration operand (``600``, ``30m``, ``1.5h``)."""
    body = word.removesuffix("s").removesuffix("m").removesuffix("h").removesuffix("d")
    return bool(body) and body.replace(".", "", 1).isdigit()


def _shape_of(command: str, depth: int = 0) -> _WaitShape:
    """The wait shape of *command*, following shell-runner nesting to ``_MAX_NESTING``."""
    shape = _WaitShape(has_loop=False, has_sleep=False, has_bound=_has_bound_marker(command))
    for segment in split_commands(tokenize(command)):
        words = [token.value for token in segment if token.kind is TokenKind.WORD]
        effective, timed = _strip_leading_noise(words)
        shape = shape.merged_with(_WaitShape(has_loop=False, has_sleep=False, has_bound=timed))
        if not effective:
            continue
        head = effective[0]
        shape = shape.merged_with(
            _WaitShape(
                has_loop=head in _UNBOUNDED_LOOP_KEYWORDS,
                has_sleep=head == "sleep",
                has_bound=False,
            )
        )
        if head in _SHELL_RUNNERS and depth < _MAX_NESTING:
            for nested in effective[1:]:
                shape = shape.merged_with(_shape_of(nested, depth + 1))
    return shape


def _has_bound_marker(command: str) -> bool:
    """Whether *command* names an elapsed-time source or the session pid a bound is built from."""
    return any(marker in command for marker in _BOUND_MARKERS)


def detect_unbounded_wait(command: str) -> UnboundedWaitDetection:
    """Return a detection for *command*; ``is_unbounded_wait`` True iff it waits forever.

    A command is an unbounded wait when it opens an ``until``/``while`` loop whose
    body sleeps and carries no bound in any of the recognised forms (a ``timeout``
    wrapper, the ``SECONDS`` builtin, a ``date +%s`` epoch deadline, or a ``$PPID``
    test tying the loop to its own session's lifetime). ``timeout`` is recognised at
    a command position — including as the wrapper around a ``bash -c`` that holds the
    loop — so a bounded wait written in any of those shapes passes through untouched.
    """
    if not command.strip():
        return UnboundedWaitDetection(is_unbounded_wait=False, message="")
    shape = _shape_of(command)
    if shape.has_loop and shape.has_sleep and not shape.has_bound:
        return UnboundedWaitDetection(is_unbounded_wait=True, message=_BLOCK_MSG)
    return UnboundedWaitDetection(is_unbounded_wait=False, message="")


__all__ = ["UnboundedWaitDetection", "detect_unbounded_wait"]
