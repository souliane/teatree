"""Always-allowed self-rescue commands (NEVER-LOCKOUT contract).

A gate that can deny the very command an operator uses to disable it is a
deadlock — the factory has wedged itself twice on exactly this
(souliane/teatree#1472, #1474). This module names the small, fixed set of
commands EVERY gate and EVERY hook must let through unconditionally, no
matter how a gate's detection misbehaves:

- DB migrate (``t3 <overlay> db migrate`` / ``worktree provision`` /
    ``manage.py migrate``): bring a wedged schema forward so the rest of the
    CLI (and the gates that shell it) work again.
- Gate disables (``t3 <overlay> gate ... disable``): the orchestrator-Bash
    and skill-loading kill-switches (#1474).
- The fail-open toggle (``t3 review gate fail-open enable``): the master
    switch that flips every over-deny gate to fail-open at once.

:func:`is_self_rescue` is pure detection over the FIRST command segment
only (lexed via the shared :mod:`teatree.hooks._command_parser`), so a
self-rescue prefix glued to a second command through a shell separator
(``; && || |`` / newline) can never smuggle a blocked command past a gate.
Each allowlist entry is an ORDERED SUBSEQUENCE of WORD tokens: an entry
matches when its words appear in order anywhere in the first segment, so
the overlay name that sits between ``t3`` and the verb is irrelevant
(``t3 acme gate disable`` and ``t3 t3-teatree gate disable`` both match
``("t3", "gate", "disable")``).
"""

from typing import Final

from teatree.hooks._command_parser import first_segment_words

# Each tuple is an ordered subsequence of WORD tokens. The overlay name
# (``acme`` / ``t3-teatree`` / …) sits between ``t3`` and the verb and is
# intentionally NOT part of any phrase, so a single entry covers every
# overlay. ``manage.py migrate`` is the raw-Django escape for a wedged DB.
SELF_RESCUE_ALLOWLIST: Final[tuple[tuple[str, ...], ...]] = (
    ("t3", "db", "migrate"),
    ("t3", "worktree", "provision"),
    ("manage.py", "migrate"),
    ("t3", "gate", "disable"),
    ("t3", "gate", "fail-open", "enable"),
)


def _is_subsequence(needle: tuple[str, ...], haystack: list[str]) -> bool:
    """True iff every word in ``needle`` appears in ``haystack`` in order."""
    it = iter(haystack)
    return all(word in it for word in needle)


def is_self_rescue(command: str) -> bool:
    """Return True iff ``command``'s FIRST segment is a self-rescue command.

    Only the first command segment is inspected (via
    :func:`first_segment_words`): a self-rescue phrase living after a shell
    separator is part of a SECOND command and must not whitelist the leading
    (possibly blocked) one. A match means NO gate and NO hook may deny this
    call — it is the operator's guaranteed escape from a lockout.
    """
    if not command:
        return False
    words = first_segment_words(command)
    if not words:
        return False
    return any(_is_subsequence(entry, words) for entry in SELF_RESCUE_ALLOWLIST)
