"""The clause a task minted from a hold carries: read the branch and the issue as they are now, then act."""

from collections.abc import Mapping
from typing import Final

REREAD_FIRST_CLAUSE: Final[str] = (
    "Re-read first: this task was minted from a hold, so the branch and the issue may have moved since. "
    "Read the branch's current HEAD and the issue's latest text before acting. "
    "If the branch already holds the fix, make no new commit: return fix_record.already_fixed "
    "with verified_head (the HEAD you read) and evidence (the passing test or diff that proves it)."
)

_HOLD_EXTRA_KEYS: Final[tuple[str, ...]] = ("shipping_hold",)


def reread_first_reason(reason: str, extra: Mapping[str, object] | None) -> str:
    """*reason* with the re-read-first clause appended when the ticket's *extra* records a hold."""
    if not extra or REREAD_FIRST_CLAUSE in reason:
        return reason
    if not any(key in extra for key in _HOLD_EXTRA_KEYS):
        return reason
    return f"{reason}\n{REREAD_FIRST_CLAUSE}"
