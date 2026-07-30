"""Detect a git command that silences hooks or schedules an out-of-band auto-merge.

The safety-critical subset of the ``hooks/scripts/direct_command_guard`` denylist
that must ALSO refuse on Lane B (the ``pydantic_ai`` shell): a command that
disables the git verification hooks (``--no-verify``, the ``git -c
core.hooksPath=…`` silencer), skips commit signing (``--no-gpg-sign``), or
schedules a GitLab pipeline-succeeds auto-merge (``git push -o
merge_request.merge_when_pipeline_succeeds``) — each a way to land work past the
gates the keystone merge enforces. The full direct-command denylist (docker,
npm, pip, playbook workflow bypasses) is a workflow-convention gate that stays
in the PreToolUse guard; only this hook/merge-bypass family is a hard-deny the
headless shell shares.

Pure and self-contained (stdlib-only) so it is importable by Lane B AND by the
cold PreToolUse subprocess. VALUE/CONFIG patterns (``core.hooksPath=``, the
push-option value) are scanned against the RAW command so quoting cannot evade
them; the flag patterns (``--no-verify`` / ``--no-gpg-sign``) are scanned against
a quote-stripped copy so the phrase inside a commit message / grep argument does
not false-fire — the exact split
:func:`hooks.scripts.direct_command_guard.deny_match` uses, so the
:mod:`teatree.hooks.hard_deny_registry` deny-corpus parity test binds the two.
"""

import re

_HOOKS_PATH_REASON = (
    "BLOCKED: `git -c core.hooksPath=…` bypasses git hooks (equivalent to `--no-verify`) — "
    "fix the hook failure instead."
)
_AUTO_MERGE_REASON = (
    "BLOCKED: `git push -o merge_request.merge_when_pipeline_succeeds` schedules an auto-merge "
    "bypassing the FSM keystone — use `t3 <overlay> ticket merge` instead."
)
_NO_VERIFY_REASON = "BLOCKED: `--no-verify` — fix the hook failure instead of bypassing it."
_NO_GPG_REASON = "BLOCKED: `--no-gpg-sign` — do not bypass signing without explicit user approval."

# VALUE/CONFIG patterns — scanned against the RAW command (quoting cannot evade).
_RAW_SCAN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\b.*-c\s+['\"]?core\.hooksPath\s*=", re.IGNORECASE), _HOOKS_PATH_REASON),
    (
        re.compile(
            r"\bgit\s+push\b.*"
            r"(?:-o\s+['\"]?merge_request\.merge_when_pipeline_succeeds"
            r"|--push-option=['\"]?merge_request\.merge_when_pipeline_succeeds)"
        ),
        _AUTO_MERGE_REASON,
    ),
)

# TOOL-INVOCATION flag patterns — scanned against a quote-stripped copy so a flag
# mentioned inside a commit message / grep argument does not false-block.
_FLAG_SCAN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\s+\S+.*--no-verify\b"), _NO_VERIFY_REASON),
    (re.compile(r"\bgit\s+\S+.*--no-gpg-sign\b"), _NO_GPG_REASON),
)

_QUOTED_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def git_bypass_deny_reason(command: str) -> str | None:
    """Return the deny reason for a hook/merge-bypass git command, or ``None``.

    ``None`` for anything that does not silence hooks, skip signing, or schedule an
    auto-merge — including ordinary git ops and prose that merely mentions a flag
    inside a quoted argument.
    """
    if not command:
        return None
    for pattern, reason in _RAW_SCAN:
        if pattern.search(command):
            return reason
    quote_stripped = _QUOTED_LITERAL_RE.sub(" ", command)
    for pattern, reason in _FLAG_SCAN:
        if pattern.search(quote_stripped):
            return reason
    return None


__all__ = ["git_bypass_deny_reason"]
