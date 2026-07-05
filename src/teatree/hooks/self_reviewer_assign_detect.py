"""Detect a Bash command that directly assigns a reviewer on an MR/PR.

The pure Bash-surface matcher behind the PreToolUse self-reviewer-assign gate
(``hooks/scripts/no_self_reviewer_assign.py``), carried in a :mod:`teatree.hooks`
leaf so BOTH the cold PreToolUse subprocess AND Lane B's shared hard-deny registry
refuse the SAME set. Reviewers are NEVER directly assigned — review is REQUESTED
via the Slack/approval channel; teatree has no sanctioned direct-assignment path.
The matcher covers the CLI (``glab mr create/update --reviewer``, ``gh pr
create/edit --reviewer``/``-r``/``--add-reviewer``) and the out-of-band REST WRITE
that sets ``reviewer_ids``/``reviewers``/``requested_reviewers`` (a GET read of the
same field is allowed — the block is gated on the effective HTTP method).

The MCP-tool surface (``mcp__glab__glab_mr_*`` carrying a reviewer arg) stays in
the PreToolUse guard — it is not a shell command and Lane B's MCP toolsets are
read-only. Detection runs on the verb-skeleton (quoted spans + heredoc bodies
stripped) so the phrase inside a commit message / doc string cannot false-fire —
the same shape ``mr_cli_fields.strip_quoted_and_heredoc`` uses, carried
self-contained here; the deny-corpus parity test binds this leaf to
``hooks.scripts.no_self_reviewer_assign._bash_assigns_reviewer``. Pure and
stdlib-only.
"""

import re

_GLAB_MR_OP_RE = re.compile(r"\bglab\s+mr\s+(?:create|update)\b")
_GH_PR_OP_RE = re.compile(r"\bgh\s+pr\s+(?:create|edit)\b")
_REVIEWER_FLAG_RE = re.compile(r"--reviewers?\b")
_GH_REVIEWER_FLAG_RE = re.compile(r"--(?:add-)?reviewers?\b|(?<![\w-])-r\b")
_API_VERB_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
_API_REVIEWER_FIELD_RE = re.compile(r"\b(?:reviewer_ids|reviewers|requested_reviewers)\b")
_API_WRITE_METHOD_RE = re.compile(r"(?:--method[ =]+|-X[ =]?)(?P<m>[A-Za-z]+)")
_API_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_BODY_FIELD_RE = re.compile(r"(?:--raw-field|--field|-[fF])\b")

# Verb-skeleton strip (heredoc bodies, then quoted spans) — mirrors
# ``mr_cli_fields.strip_quoted_and_heredoc``.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(?P<delim>\w+)\1.*?^\s*(?P=delim)\b", re.DOTALL | re.MULTILINE)
_SQUOTE_SPAN_RE = re.compile(r"'[^']*'")
_DQUOTE_SPAN_RE = re.compile(r'"[^"]*"')

_REVIEWER_ASSIGN_DENY_REASON = (
    "BLOCKED: teatree NEVER directly assigns a reviewer on an MR/PR — least of "
    "all the user's OWN MR. Review is REQUESTED via the Slack/approval channel "
    "only (post the MR link to the review channel; the reviewer self-claims). "
    "There is no sanctioned direct-assignment path: do not run "
    "`glab mr create/update --reviewer`, `gh pr create --reviewer`/`-r`, "
    "`gh pr edit --add-reviewer`, and do not set `reviewer_ids`/`requested_reviewers` "
    "via a write API call."
)


def _strip_quoted_and_heredoc(command: str) -> str:
    """Command with heredoc bodies and quoted spans removed — for verb DETECTION."""
    without_heredoc = _HEREDOC_RE.sub(" ", command)
    without_squote = _SQUOTE_SPAN_RE.sub(" ", without_heredoc)
    return _DQUOTE_SPAN_RE.sub(" ", without_squote)


def _api_call_writes_reviewer(skeleton: str) -> bool:
    """Whether a ``gh``/``glab api`` call is a reviewer-list WRITE (not a GET read)."""
    if not (_API_VERB_RE.search(skeleton) and _API_REVIEWER_FIELD_RE.search(skeleton)):
        return False
    method_match = _API_WRITE_METHOD_RE.search(skeleton)
    if method_match:
        return method_match.group("m").upper() in _API_WRITE_METHODS
    return bool(_API_BODY_FIELD_RE.search(skeleton))


def bash_assigns_reviewer(command: str) -> bool:
    """Whether a Bash command directly assigns a reviewer on an MR/PR."""
    if not command:
        return False
    skeleton = _strip_quoted_and_heredoc(command)
    if _GLAB_MR_OP_RE.search(skeleton) and _REVIEWER_FLAG_RE.search(skeleton):
        return True
    if _GH_PR_OP_RE.search(skeleton) and _GH_REVIEWER_FLAG_RE.search(skeleton):
        return True
    return _api_call_writes_reviewer(skeleton)


def reviewer_assign_deny_reason(command: str) -> str | None:
    """Return the deny reason for a direct reviewer-assign command, or ``None``."""
    if not bash_assigns_reviewer(command):
        return None
    return _REVIEWER_ASSIGN_DENY_REASON


__all__ = ["bash_assigns_reviewer", "reviewer_assign_deny_reason"]
