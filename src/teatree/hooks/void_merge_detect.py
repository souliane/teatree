"""Detect an executed ``pr void-merge`` that would write — a merge void is typed by a human, never run by an agent.

Read-only forms stay open: ``--dry-run`` and ``--help`` write nothing. An invocation,
never a mention: the shared :mod:`teatree.hooks.forge_subcommand` traversal reads only
executed command segments, so a ``grep void-merge`` or an ``echo`` of the command passes.
"""

from teatree.hooks.forge_subcommand import forge_program_argvs

_LAUNCHERS: frozenset[str] = frozenset({"t3", "python", "python3", "manage.py", "uv", "uvx", "django-admin"})
_READ_ONLY_FLAGS: frozenset[str] = frozenset({"--dry-run", "--help", "-h"})

DENY_REASON = (
    "BLOCKED: `pr void-merge` rewrites a recorded merge, so only a human at a terminal runs it. "
    "Ask the operator to type `t3 <overlay> pr void-merge <pr-url> --reason '<why>' --by <their name>` "
    "in their own shell; it shows the forge evidence and asks them to type yes. "
    "`--dry-run` is read-only and stays available here."
)


def _writes_a_void(words: list[str]) -> bool:
    if "void-merge" not in words:
        return False
    tail = words[words.index("void-merge") :]
    return "pr" in words[: words.index("void-merge")] and not _READ_ONLY_FLAGS.intersection(tail)


def void_merge_deny_reason(command: str) -> str | None:
    return DENY_REASON if any(_writes_a_void(words) for words in forge_program_argvs(command, _LAUNCHERS)) else None
