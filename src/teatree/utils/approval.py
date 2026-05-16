"""Per-invocation interactive approval gate (#777).

The safety mechanism for privileged, expensive, or destructive
operations that must never run unattended — e.g. pulling a fresh dump
from a remote DEV environment. Replaces the blanket
``T3_ALLOW_REMOTE_DUMP``/``allow_remote_dump=False`` prohibition
anti-pattern: instead of permanently disabling the path, every
invocation requires a fresh, explicit, human confirmation.

The gate is designed so an unattended agent cannot self-approve: it
refuses unless **both** streams are real TTYs and the human types an
affirmative answer. An agent piping ``y`` into a non-TTY stdin is the
exact scenario this blocks.
"""

from typing import TextIO

_AFFIRMATIVE = {"y", "yes"}


class ApprovalRefusedError(RuntimeError):
    """Raised when a per-invocation approval gate is not satisfied."""


def require_interactive_approval(prompt: str, *, stdin: TextIO, stdout: TextIO) -> None:
    """Block until the human explicitly approves, else raise ``ApprovalRefusedError``.

    Both *stdin* and *stdout* must be interactive TTYs — in a headless /
    agent context one of them is not, so the gate refuses with a message
    stating a human must run the command. A real human at a terminal must
    type ``y``/``yes`` (anything else, including the empty default, is a
    refusal — fail closed).
    """
    if not (stdin.isatty() and stdout.isatty()):
        msg = (
            "Approval required but no interactive terminal is attached. "
            "This operation cannot be self-approved by an unattended agent — "
            "a human must run this command in an interactive shell and approve it."
        )
        raise ApprovalRefusedError(msg)

    stdout.write(f"{prompt}\n")
    stdout.write("Type 'yes' to approve, anything else aborts: ")
    stdout.flush()
    answer = stdin.readline().strip().lower()
    if answer not in _AFFIRMATIVE:
        msg = f"Approval declined (answer: {answer!r}). Operation aborted."
        raise ApprovalRefusedError(msg)
