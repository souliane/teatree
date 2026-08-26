"""PreToolUse: never directly assign a reviewer on a GitLab/GitHub MR/PR.

Reviewers must NEVER be directly assigned on an MR — least of all the user's
OWN MR (this happened on the user's MRs and is forbidden). Colleague review is
*requested* via the Slack/approval channel only, so the gate blocks the action
itself rather than attempting a fragile, network-bound author lookup inside a
30s hook. The overlay's standing ``pr_auto_reviewers`` policy is the one
assignment that exists, and it is unreachable from here: it rides the MR-create
POST, or ``review apply-reviewer-policy``, which is neither a forge CLI nor a
REST leader and so matches nothing below.

The gate watches every reviewer-assignment surface:

* the CLI ``glab mr create``/``update <iid> --reviewer/--reviewers <user>`` (the
    update surface drove the bug; create assigns the reviewer at creation);
* the GitHub CLI ``gh pr create --reviewer/-r`` and ``gh pr edit
    --add-reviewer/--reviewer`` (the GitHub siblings of the glab verbs);
* the out-of-band ``glab api``/``gh api``/``curl`` WRITE that sets ``reviewer_ids``
    / ``reviewers`` / ``requested_reviewers`` on a ``merge_requests``/``pulls``
    endpoint (the web-UI-equivalent edit that bypasses the CLI) — a GET read of
    the same field (e.g. ``gh api .../requested_reviewers`` to LIST them) is
    allowed, the block is gated on the effective HTTP method;
* the ``mcp__glab__glab_mr_update``/``mcp__glab__glab_mr_create`` MCP tools
    carrying a ``reviewer`` arg.

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

from hooks.scripts.mr_cli_fields import strip_quoted_and_heredoc

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("no_self_reviewer_assign", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.no_self_reviewer_assign", sys.modules[__name__])

# A real ``glab mr create``/``update`` carrying a reviewer flag — BOTH verbs
# assign a reviewer (``create --reviewer`` sets it at creation, ``update
# --reviewer`` mutates an existing MR). Matched against the command with quoted
# spans and heredoc bodies stripped so the phrase embedded in a commit message /
# doc string is not a false fire.
_GLAB_MR_OP_RE = re.compile(r"\bglab\s+mr\s+(?:create|update)\b")
# A real ``gh pr create``/``edit`` — the GitHub siblings of the glab verbs.
# ``gh pr create --reviewer/-r`` and ``gh pr edit --add-reviewer`` both assign.
_GH_PR_OP_RE = re.compile(r"\bgh\s+pr\s+(?:create|edit)\b")
# ``--reviewer``/``--reviewers`` (with or without ``=``/value) on the glab CLI.
_REVIEWER_FLAG_RE = re.compile(r"--reviewers?\b")
# The ``gh pr`` reviewer flags: ``--reviewer``/``-r`` (create & edit) and
# ``--add-reviewer`` (edit). ``-r`` is the short form ``gh pr create`` accepts.
_GH_REVIEWER_FLAG_RE = re.compile(r"--(?:add-)?reviewers?\b|(?<![\w-])-r\b")
# An out-of-band REST write that sets the reviewer list — GitLab
# (``reviewer_ids``/``reviewers``) or GitHub (``requested_reviewers`` endpoint
# / ``reviewers`` field). Matched on a ``glab api``/``gh api`` command.
_API_VERB_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
# The forge CLIs are not the only client of that REST field: a plain ``curl -X PUT``
# sets ``reviewer_ids`` just as well, and answered to no leader this gate matched.
_CURL_VERB_RE = re.compile(r"(?<![\w-])curl\b")
# A reviewer-list reference that is an ARGUMENT, not prose: a field ASSIGNMENT
# (``-f reviewer_ids=42``), a JSON body KEY (``-d '{"reviewer_ids": [42]}'``), or
# the endpoint SUB-RESOURCE (``.../pulls/12/requested_reviewers``). Matched
# against the RAW command, never the verb skeleton — a field value is an ordinary
# quoted shell argument, so the skeleton's quote strip erased
# ``-f 'reviewer_ids=42'`` and the reviewer WRITE passed as a non-reviewer call.
_API_REVIEWER_FIELD_RE = re.compile(
    r"\b(?:reviewer_ids|reviewers|requested_reviewers)(?:\[\])?\s*="
    r"|['\"](?:reviewer_ids|reviewers|requested_reviewers)['\"]\s*:"
    r"|/(?:reviewers|requested_reviewers)\b"
)
# A WRITE HTTP method on a REST call — ``--method PUT/POST/PATCH`` / ``-X POST`` /
# ``-XPOST``, plus curl's own ``--request`` spelling. A GET (the default, or
# explicit) is a READ of the reviewer list, not an assignment, and must NOT be
# blocked (e.g. ``gh api .../requested_reviewers`` lists the requested reviewers).
# Every spelling is captured and resolved LAST-WINS, matching pflag's semantics.
_API_WRITE_METHOD_RE = re.compile(r"(?:-X|--method|--request)[\s=]+['\"]?([A-Za-z]+)\b|(?<=-X)([A-Za-z]+)\b")
_API_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# A body-field flag — ``-f``/``--field``/``--raw-field``/``-F`` — which the
# GitHub CLI uses to turn a default GET into a POST (implied write).
_API_BODY_FIELD_RE = re.compile(r"(?:--raw-field|--field|-[fF])\b")
# curl's own implied-write flags. ``-f`` is DELIBERATELY absent: under curl it is
# ``--fail``, so admitting it would block an ordinary ``curl -fsSL`` read. The short
# forms allow a cluster (``-sSLd``), which curl accepts when the value-taking flag
# is last.
_CURL_BODY_FLAG_RE = re.compile(
    r"--data(?:-raw|-binary|-urlencode|-ascii)?\b|--json\b|--form(?:-string)?\b|--upload-file\b"
    r"|(?<![\w-])-[a-zA-Z]*[dFT]\b"
)
# Per-call escape, mirroring the other gates' ``[…-ok: <reason>]`` tokens.
_REVIEWER_OK_RE = re.compile(r"\[reviewer-ok:\s*(\S[^\]]*?)\s*\]")

# The MCP MR-create/update tools — a reviewer arg on either assigns directly.
_MCP_UPDATE_TOOLS = ("mcp__glab__glab_mr_update", "mcp__glab__glab_mr_create")
_MCP_REVIEWER_KEYS = ("reviewer", "reviewers", "reviewer_ids", "reviewer_username", "requested_reviewers")

_REASON = (
    "BLOCKED: teatree NEVER directly assigns a reviewer on an MR/PR — least of "
    "all the user's OWN MR. Review is REQUESTED via the Slack/approval channel "
    "only (post the MR link to the review channel; the reviewer self-claims). "
    "The ONE sanctioned path is `t3 <overlay> review apply-reviewer-policy`, which "
    "applies the overlay's configured `pr_auto_reviewers` to the factory bot's own "
    "open MRs — it names no username, so it cannot be aimed at a colleague. "
    "Do not run "
    "`glab mr create/update --reviewer`, `gh pr create --reviewer`/`-r`, "
    "`gh pr edit --add-reviewer`, do not set `reviewer_ids`/`requested_reviewers` "
    "via a write API call (`gh`/`glab api` OR a raw `curl`), and do not pass a "
    "reviewer arg to the MR-create/update MCP tool. If this is a vetted one-off "
    "on a COLLEAGUE's MR, put "
    "`[reviewer-ok: <reason>]` in the Bash `command` string, or in any string "
    "argument of the MCP call (the Bash `description` field is NOT scanned)."
)


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] no_self_reviewer_assign_gate_enabled = false``) is the
    one-line kill-switch.
    """
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

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


def _body_flag_pattern(skeleton: str) -> "re.Pattern[str] | None":
    """The implied-write flags of the REST client *skeleton* invokes, else ``None``.

    Each client spells them differently and ``-f`` means opposite things (a gh
    field, curl's ``--fail``), so the leader picks the pattern.
    """
    if _API_VERB_RE.search(skeleton):
        return _API_BODY_FIELD_RE
    return _CURL_BODY_FLAG_RE if _CURL_VERB_RE.search(skeleton) else None


def _rest_call_writes_reviewer(skeleton: str, command: str) -> bool:
    """Whether a REST call (``gh``/``glab api`` or ``curl``) is a reviewer WRITE.

    A reviewer field on the path/args is necessary but not sufficient: a plain
    ``gh api .../requested_reviewers`` is a GET that LISTS the requested
    reviewers — a read, not an assignment. The write is gated on the effective
    HTTP method: an explicit write verb (``--method``/``--request``/``-X`` with
    POST/PUT/PATCH/DELETE), or an implicit POST inferred from the client's own
    body flag. With neither, the call is a GET and is allowed (read-path
    never-lockout).

    The VERB and the body flag are read off the ``skeleton`` — invocation
    syntax a quoted mention must not fire the gate on. The reviewer FIELD and the
    method VALUE are read off the raw ``command``, because both are argument
    values a shell quotes routinely and the skeleton erases them. The effective
    method is LAST-WINS, as pflag resolves a repeated flag.
    """
    body_flags = _body_flag_pattern(skeleton)
    if body_flags is None or not _API_REVIEWER_FIELD_RE.search(command):
        return False
    methods = [m.upper() for pair in _API_WRITE_METHOD_RE.findall(command) for m in pair if m]
    if methods:
        return methods[-1] in _API_WRITE_METHODS
    return bool(body_flags.search(skeleton))


def _bash_assigns_reviewer(command: str) -> bool:
    """Whether a Bash command directly assigns a reviewer on an MR/PR.

    The VERB is detected on the verb-skeleton (quoted spans + heredoc bodies
    stripped) so the phrase inside a commit message / doc string cannot
    false-fire:

    * ``glab mr create``/``update`` carrying ``--reviewer``/``--reviewers``;
    * ``gh pr create``/``edit`` carrying ``--reviewer``/``-r``/``--add-reviewer``;
    * a ``glab api``/``gh api``/``curl`` WRITE setting ``reviewer_ids``/``reviewers``/
        ``requested_reviewers`` (a GET read of the same field is allowed —
        see :func:`_rest_call_writes_reviewer`, which reads the field and the
        method off the RAW command since both are quotable argument values).
    """
    skeleton = strip_quoted_and_heredoc(command)
    if _GLAB_MR_OP_RE.search(skeleton) and _REVIEWER_FLAG_RE.search(skeleton):
        return True
    if _GH_PR_OP_RE.search(skeleton) and _GH_REVIEWER_FLAG_RE.search(skeleton):
        return True
    return _rest_call_writes_reviewer(skeleton, command)


def _mcp_assigns_reviewer(data: dict) -> bool:
    """Whether an MCP MR-create/update tool carries a non-empty reviewer arg."""
    if data.get("tool_name") not in _MCP_UPDATE_TOOLS:
        return False
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return False
    return any(tool_input.get(key) for key in _MCP_REVIEWER_KEYS)


def _mcp_reviewer_ok_token(tool_input: dict) -> str | None:
    """The ``[reviewer-ok: <reason>]`` reason from any string arg of an MCP call.

    An MCP tool call carries no ``command``, so the Bash-only scanner left this
    branch advertising an escape that could not be placed anywhere the gate reads
    — a never-lockout contract the gate did not actually honour. Every string
    argument is scanned because the MCP schema fixes no field for free text.
    """
    for value in tool_input.values():
        if not isinstance(value, str):
            continue
        if reason := _reviewer_ok_token(value):
            return reason
    return None


def handle_block_self_reviewer_assign(data: dict) -> bool:
    """Block any direct reviewer-assignment surface — review is requested, never assigned.

    Fires when the gate is enabled (kill-switch off), no per-call
    ``[reviewer-ok: <reason>]`` token is present, and the call is a
    reviewer-assignment surface (CLI ``glab mr create``/``update --reviewer``,
    ``gh pr create``/``edit`` with a reviewer flag, an out-of-band
    ``glab api``/``gh api`` reviewer-field WRITE, or the MCP MR-create/update
    tool carrying a reviewer arg). Every other call ALLOWS — including a GET
    read of the reviewer list. The deny routes through
    :func:`_fail_open_or_deny` so the self-rescue allowlist + master fail-open
    switch + circuit breaker all apply (never-lockout).
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if not _gate_enabled():
        return False

    if data.get("tool_name", "") == "Bash":
        command = data.get("tool_input", {}).get("command", "")
        if not command or not _bash_assigns_reviewer(command):
            return False
        reason = _reviewer_ok_token(command)
    elif _mcp_assigns_reviewer(data):
        reason = _mcp_reviewer_ok_token(data.get("tool_input", {}))
    else:
        return False

    # Both surfaces waive on the same token and deny the same way; resolving the surface
    # first keeps that decision in one place instead of once per branch.
    if reason:
        sys.stderr.write(f"NOTE: reviewer-assign gate skipped via [reviewer-ok: {reason}].\n")
        return False
    return _fail_open_or_deny(data, _REASON)
