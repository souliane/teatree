"""Detect a Bash command that directly assigns a reviewer on an MR/PR.

The pure Bash-surface matcher behind the PreToolUse self-reviewer-assign gate
(``hooks/scripts/no_self_reviewer_assign.py``), carried in a :mod:`teatree.hooks`
leaf so BOTH the cold PreToolUse subprocess AND Lane B's shared hard-deny registry
refuse the SAME set. Colleague review is REQUESTED via the Slack/approval channel,
never assigned; the one sanctioned assignment is the overlay's own standing policy
(``review apply-reviewer-policy``), which names no username and no forge client.
The matcher covers the CLI (``glab mr create/update --reviewer``, ``gh pr
create/edit --reviewer``/``-r``/``--add-reviewer``) and the out-of-band REST WRITE
that sets ``reviewer_ids``/``reviewers``/``requested_reviewers`` — through the forge
CLIs OR a raw ``curl``, which reaches the same field and answered to no leader the
matcher had (a GET read of either is allowed — the block is gated on the effective
HTTP method).

The MCP-tool surface (``mcp__glab__glab_mr_*`` carrying a reviewer arg) stays in
the PreToolUse guard — it is not a shell command and Lane B's MCP toolsets are
read-only. VERB detection runs on the verb-skeleton (quoted spans + heredoc
bodies stripped) so the phrase inside a commit message / doc string cannot
false-fire — the same shape ``mr_cli_fields.strip_quoted_and_heredoc`` uses,
carried self-contained here. Argument VALUES — the reviewer field and the HTTP
method — are read off the RAW command instead, since quoting an argument is
ordinary shell and the skeleton erases it; the argument SHAPES the field pattern
requires are what keep the raw read off prose. The deny-corpus parity test binds
this leaf to ``hooks.scripts.no_self_reviewer_assign._bash_assigns_reviewer``.
Pure and stdlib-only.
"""

import re
import shlex

_GLAB_MR_OP_RE = re.compile(r"\bglab\s+mr\s+(?:create|update)\b")
_GH_PR_OP_RE = re.compile(r"\bgh\s+pr\s+(?:create|edit)\b")
_REVIEWER_FLAG_RE = re.compile(r"--reviewers?\b")
_GH_REVIEWER_FLAG_RE = re.compile(r"--(?:add-)?reviewers?\b|(?<![\w-])-r\b")
_API_VERB_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
_CURL_VERB_RE = re.compile(r"(?<![\w-])curl\b")
# A reviewer-list reference that is an ARGUMENT, not prose: a field ASSIGNMENT
# (``-f reviewer_ids=42``, ``--raw-field 'reviewers[]=bob'``), a JSON body KEY
# (``-d '{"reviewer_ids": [42]}'``), or the endpoint SUB-RESOURCE
# (``.../pulls/12/requested_reviewers``). Matched against the RAW command, never
# the verb skeleton: a field value is an ordinary quoted shell argument, so the
# skeleton's quote strip erased ``-f 'reviewer_ids=42'`` and the reviewer WRITE
# passed as a non-reviewer call. The argument shapes are what keep the raw match
# off a comment body that merely mentions reviewers.
_API_REVIEWER_FIELD_RE = re.compile(
    r"\b(?:reviewer_ids|reviewers|requested_reviewers)(?:\[\])?\s*="
    r"|['\"](?:reviewer_ids|reviewers|requested_reviewers)['\"]\s*:"
    r"|/(?:reviewers|requested_reviewers)\b"
)
# The method flag in every empirically-valid spelling — spaced/``=`` (``-X PUT``,
# ``--method=POST``, curl's ``--request PUT``, ``-X 'PATCH'``) and the pflag no-space
# shorthand (``-XPUT``). Consumers flatten the two groups and keep LAST-WINS, matching
# pflag: a repeated flag overwrites, so reading the FIRST let ``-X GET … -X PATCH``
# classify a real reviewer write as a read.
_API_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_BODY_FIELD_RE = re.compile(r"(?:--raw-field|--field|-[fF])\b")
# curl's own implied-write flags. ``-f`` is DELIBERATELY absent: under curl it is
# ``--fail``, so admitting it would block an ordinary ``curl -fsSL`` read. The short
# forms allow a cluster (``-sSLd``), which curl accepts when the value-taking flag
# is last.
_CURL_BODY_FLAG_RE = re.compile(
    r"--data(?:-raw|-binary|-urlencode|-ascii)?\b|--json\b|--form(?:-string)?\b|--upload-file\b"
    r"|(?<![\w-])-[a-zA-Z]*[dFT]\b"
)

# Verb-skeleton strip (heredoc bodies, then quoted spans) — mirrors
# ``mr_cli_fields.strip_quoted_and_heredoc``.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(?P<delim>\w+)\1.*?^\s*(?P=delim)\b", re.DOTALL | re.MULTILINE)
_SQUOTE_SPAN_RE = re.compile(r"'[^']*'")
_DQUOTE_SPAN_RE = re.compile(r'"[^"]*"')

_REVIEWER_ASSIGN_DENY_REASON = (
    "BLOCKED: teatree NEVER directly assigns a reviewer on an MR/PR — least of "
    "all the user's OWN MR. Review is REQUESTED via the Slack/approval channel "
    "only (post the MR link to the review channel; the reviewer self-claims). "
    "The ONE sanctioned path is `t3 <overlay> review apply-reviewer-policy`, which "
    "applies the overlay's configured `pr_auto_reviewers` to the factory bot's own "
    "open MRs — it names no username, so it cannot be aimed at a colleague. "
    "Do not run "
    "`glab mr create/update --reviewer`, `gh pr create --reviewer`/`-r`, "
    "`gh pr edit --add-reviewer`, and do not set `reviewer_ids`/`requested_reviewers` "
    "via a write API call (`gh`/`glab api` OR a raw `curl`)."
)


def _strip_quoted_and_heredoc(command: str) -> str:
    """Command with heredoc bodies and quoted spans removed — for verb DETECTION."""
    without_heredoc = _HEREDOC_RE.sub(" ", command)
    without_squote = _SQUOTE_SPAN_RE.sub(" ", without_heredoc)
    return _DQUOTE_SPAN_RE.sub(" ", without_squote)


def _body_flag_pattern(skeleton: str) -> re.Pattern[str] | None:
    """The implied-write flags of the REST client *skeleton* invokes, else ``None``.

    Each client spells them differently and ``-f`` means opposite things (a gh
    field, curl's ``--fail``), so the leader picks the pattern.
    """
    if _API_VERB_RE.search(skeleton):
        return _API_BODY_FIELD_RE
    return _CURL_BODY_FLAG_RE if _CURL_VERB_RE.search(skeleton) else None


#: The flags every REST client spells a method with. ``-XPOST`` (no separator) is
#: curl's own accepted form, so it is matched on the token itself.
_SHORT_METHOD_FLAG = "-X"
_METHOD_FLAGS = (_SHORT_METHOD_FLAG, "--method", "--request")
#: The separator-less and ``=`` spellings. A method is a bare word, so a token carrying
#: anything else (``-A '-X GET'``) is a flag's VALUE, never a method flag.
_ATTACHED_METHOD_RE = re.compile(r"^-X(?P<method>[A-Za-z]+)$")
_ASSIGNED_METHOD_RE = re.compile(r"^(?:-X|--method|--request)=(?P<method>[A-Za-z]+)$")


def _effective_method(argv: list[str]) -> str | None:
    """The method the client would actually SEND, or ``None`` when no token is one.

    Only a token in OPTION position counts: one whose predecessor is another option is
    that option's VALUE, and honouring any token merely BEGINNING with ``-X`` let a
    caller downgrade a real write to a read by appending ``-A '-X GET'``. Last-wins
    among the rest, matching pflag and curl, where a repeated flag overwrites.
    """
    methods: list[str] = []
    for index, token in enumerate(argv):
        previous = argv[index - 1] if index else ""
        if previous.startswith("-") and previous not in _METHOD_FLAGS:
            continue
        if token in _METHOD_FLAGS:
            if index + 1 < len(argv):
                methods.append(argv[index + 1])
        elif match := (_ATTACHED_METHOD_RE.match(token) or _ASSIGNED_METHOD_RE.match(token)):
            methods.append(match["method"])
    return methods[-1].upper() if methods else None


def _rest_call_writes_reviewer(skeleton: str, command: str) -> bool:
    """Whether a ``gh``/``glab api``/``curl`` call is a reviewer WRITE (not a GET read).

    The VERB and the body flag are read off the ``skeleton`` (they are invocation
    syntax, so a quoted mention must not fire the gate); the reviewer FIELD is read off
    the raw ``command``, because it is an argument value a shell quotes routinely. The
    method comes from the tokenised argv, last-wins as pflag resolves a repeated flag.
    A command the shell cannot tokenise at all is a WRITE: an unbalanced quote makes
    every read signal unreadable, and returning a read there opened the gate.
    """
    body_flags = _body_flag_pattern(skeleton)
    if body_flags is None or not _API_REVIEWER_FIELD_RE.search(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return True
    method = _effective_method(argv)
    if method is not None:
        return method in _API_WRITE_METHODS
    return bool(body_flags.search(skeleton))


def bash_assigns_reviewer(command: str) -> bool:
    """Whether a Bash command directly assigns a reviewer on an MR/PR."""
    if not command:
        return False
    skeleton = _strip_quoted_and_heredoc(command)
    if _GLAB_MR_OP_RE.search(skeleton) and _REVIEWER_FLAG_RE.search(skeleton):
        return True
    if _GH_PR_OP_RE.search(skeleton) and _GH_REVIEWER_FLAG_RE.search(skeleton):
        return True
    return _rest_call_writes_reviewer(skeleton, command)


def reviewer_assign_deny_reason(command: str) -> str | None:
    """Return the deny reason for a direct reviewer-assign command, or ``None``."""
    if not bash_assigns_reviewer(command):
        return None
    return _REVIEWER_ASSIGN_DENY_REASON


__all__ = ["bash_assigns_reviewer", "reviewer_assign_deny_reason"]
