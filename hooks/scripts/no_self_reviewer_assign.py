"""PreToolUse: never directly assign a reviewer on a GitLab/GitHub MR/PR.

Reviewers must NEVER be directly assigned on an MR — least of all the user's
OWN MR (this happened on the user's MRs and is forbidden). Review is
*requested* via the Slack/approval channel only; teatree has NO legitimate
direct-assignment path, so the gate blocks the action itself rather than
attempting a fragile, network-bound author lookup inside a 3-5s hook.

The gate watches every reviewer-assignment surface:

* the CLI ``glab mr update <iid> --reviewer/--reviewers <user>`` (the surface
    that drove the bug);
* the out-of-band ``glab api``/``gh api`` write that sets ``reviewer_ids`` /
    ``reviewers`` / ``requested_reviewers`` on a ``merge_requests``/``pulls``
    endpoint (the web-UI-equivalent edit that bypasses the CLI);
* the ``mcp__glab__glab_mr_update`` MCP tool carrying a ``reviewer`` arg.

This is SEPARATE from the MR-metadata gate (``handle_validate_mr_metadata``),
which deliberately SKIPS a ``--reviewer`` update (it validates only the
title/description fields a command sets — never-lockout) and so never saw the
bug. The verb itself is detected against the command with quoted spans and
heredoc bodies stripped (reusing ``mr_cli_fields.strip_quoted_and_heredoc``),
so the phrase merely embedded in a ``git commit -m '… --reviewer …'`` message
or a doc string no longer false-fires.

OPT-OUT + never-lockout: a per-call ``[reviewer-ok: <reason>]`` token and the
``no_self_reviewer_assign_gate_enabled`` kill-switch both ALLOW; the deny
routes through ``_fail_open_or_deny`` so the self-rescue allowlist + master
fail-open switch + circuit breaker all apply.

Helpers that emit the deny and read config live in ``hook_router`` and are
imported lazily at call time — ``hook_router`` imports this module at top
level, so importing it back at top level here would be a cycle.
"""

import re
import sys

from mr_cli_fields import strip_quoted_and_heredoc

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("no_self_reviewer_assign", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.no_self_reviewer_assign", sys.modules[__name__])

# A real ``glab mr update`` carrying a reviewer flag. Matched against the
# command with quoted spans and heredoc bodies stripped so the phrase embedded
# in a commit message / doc string is not a false fire.
_GLAB_MR_UPDATE_RE = re.compile(r"\bglab\s+mr\s+update\b")
# ``--reviewer``/``--reviewers`` (with or without ``=``/value) on the CLI.
_REVIEWER_FLAG_RE = re.compile(r"--reviewers?\b")
# An out-of-band REST write that sets the reviewer list — GitLab
# (``reviewer_ids``/``reviewers``) or GitHub (``requested_reviewers`` endpoint
# / ``reviewers`` field). Matched on a ``glab api``/``gh api`` command.
_API_VERB_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
_API_REVIEWER_FIELD_RE = re.compile(r"\b(?:reviewer_ids|reviewers|requested_reviewers)\b")
# Per-call escape, mirroring the other gates' ``[…-ok: <reason>]`` tokens.
_REVIEWER_OK_RE = re.compile(r"\[reviewer-ok:\s*(\S[^\]]*?)\s*\]")

# The MCP MR-update tool — a reviewer arg on it assigns directly too.
_MCP_UPDATE_TOOL = "mcp__glab__glab_mr_update"
_MCP_REVIEWER_KEYS = ("reviewer", "reviewers", "reviewer_ids", "reviewer_username", "requested_reviewers")

_REASON = (
    "BLOCKED: teatree NEVER directly assigns a reviewer on an MR/PR — least of "
    "all the user's OWN MR. Review is REQUESTED via the Slack/approval channel "
    "only (post the MR link to the review channel; the reviewer self-claims). "
    "There is no sanctioned direct-assignment path: do not run "
    "`glab mr update --reviewer`, do not set `reviewer_ids`/`requested_reviewers` "
    "via the API, and do not pass a reviewer arg to the MR-update MCP tool. "
    "If this is a vetted one-off on a COLLEAGUE's MR, append "
    "`[reviewer-ok: <reason>]` to the command."
)


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] no_self_reviewer_assign_gate_enabled = false``) is the
    one-line kill-switch.
    """
    from hook_router import _teatree_bool_setting  # noqa: PLC0415, PLC2701

    return _teatree_bool_setting("no_self_reviewer_assign_gate_enabled", default=True)


def _reviewer_ok_token(command: str) -> str | None:
    """Return the reason from a ``[reviewer-ok: <reason>]`` token, else None.

    Scanned within the first 512 chars (mirroring the other per-call escapes);
    an empty reason returns None so a bare ``[reviewer-ok: ]`` never allows.
    """
    match = _REVIEWER_OK_RE.search(command[:512])
    if not match:
        return None
    reason = match.group(1).strip()
    return reason or None


def _bash_assigns_reviewer(command: str) -> bool:
    """Whether a Bash command directly assigns a reviewer on an MR/PR.

    Two surfaces, each detected on the verb-skeleton (quoted spans + heredoc
    bodies stripped) so the phrase inside a commit message / doc string / quoted
    arg cannot false-fire:

    * ``glab mr update`` carrying ``--reviewer``/``--reviewers``;
    * ``glab api``/``gh api`` setting ``reviewer_ids``/``reviewers``/
        ``requested_reviewers``.
    """
    skeleton = strip_quoted_and_heredoc(command)
    if _GLAB_MR_UPDATE_RE.search(skeleton) and _REVIEWER_FLAG_RE.search(skeleton):
        return True
    return bool(_API_VERB_RE.search(skeleton) and _API_REVIEWER_FIELD_RE.search(command))


def _mcp_assigns_reviewer(data: dict) -> bool:
    """Whether the MCP MR-update tool carries a non-empty reviewer arg."""
    if data.get("tool_name") != _MCP_UPDATE_TOOL:
        return False
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return False
    return any(tool_input.get(key) for key in _MCP_REVIEWER_KEYS)


def handle_block_self_reviewer_assign(data: dict) -> bool:
    """Block any direct reviewer-assignment surface — review is requested, never assigned.

    Fires when the gate is enabled (kill-switch off), no per-call
    ``[reviewer-ok: <reason>]`` token is present, and the call is a
    reviewer-assignment surface (CLI ``glab mr update --reviewer``, an
    out-of-band ``glab api``/``gh api`` reviewer-field write, or the MCP
    MR-update tool carrying a reviewer arg). Every other call ALLOWS. The deny
    routes through :func:`_fail_open_or_deny` so the self-rescue allowlist +
    master fail-open switch + circuit breaker all apply (never-lockout).
    """
    from hook_router import _fail_open_or_deny  # noqa: PLC0415, PLC2701

    if not _gate_enabled():
        return False

    tool_name = data.get("tool_name", "")
    if tool_name == "Bash":
        command = data.get("tool_input", {}).get("command", "")
        if not command or not _bash_assigns_reviewer(command):
            return False
        if reason := _reviewer_ok_token(command):
            sys.stderr.write(f"NOTE: reviewer-assign gate skipped via [reviewer-ok: {reason}].\n")
            return False
        return _fail_open_or_deny(data, _REASON)

    if _mcp_assigns_reviewer(data):
        return _fail_open_or_deny(data, _REASON)
    return False
