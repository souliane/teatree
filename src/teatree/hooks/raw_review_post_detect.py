"""Detect a raw ``glab api``/``gh api`` WRITE to a review-comment endpoint.

The pure matcher behind the PreToolUse raw-review-post gate (#1164 / #2384 PR6),
carried in a :mod:`teatree.hooks` leaf so BOTH the cold PreToolUse subprocess (via
``hooks/scripts/raw_review_post_guard.py``) AND Lane B's shared hard-deny registry
refuse the SAME set — a raw REST WRITE to a ``.../discussions``/``.../notes``/
``.../comments`` endpoint, which bypasses the sanctioned CLI for that surface. A
plain GET read is allowed.

The refusal is surface-aware, because the two surfaces have different sanctioned
CLIs: a merge-request note goes to the top-level ``t3 review post-comment`` (draft-default,
dedup, on-behalf approval), while an issue/work-item note goes to
``t3 <overlay> ticket comment`` — the ``t3 review`` create seam is merge-request-shaped
and cannot address an issue at all. Naming one remedy for both blocked the caller and
then sent them to a command that could not do the job.

The command is classified by its EFFECTIVE HTTP method — the LAST ``-X``/
``--method`` value wins; with no method flag the forge defaults to POST when a
body/field flag is present, else GET. Only a GET is a read. The method regexes are
carried self-contained here (the same shapes ``hook_router`` keeps for its own
gates), so the leaf stays importable by Lane B; the deny-corpus parity test binds
this leaf to ``hooks.scripts.raw_review_post_guard.is_raw_review_write``. Pure and
stdlib-only.
"""

import re

REVIEW_POST_ENDPOINT_RE = re.compile(r"(?:merge_requests|pulls|issues)/\d+/(?:discussions|notes|comments)\b")
ISSUE_NOTE_ENDPOINT_RE = re.compile(r"issues/\d+/(?:notes|comments)\b")
_GLAB_GH_API_RE = re.compile(r"\b(?:glab|gh)\s+api\b")
_METHOD_FLAG_RE = re.compile(r"(?:-X|--method)[\s=]+['\"]?([A-Za-z]+)\b|(?<=-X)([A-Za-z]+)\b")
_BODY_FLAG_RE = re.compile(r"(?:^|\s)(?:-f|--field|-F|--raw-field|--input|-d|--data)\b")

MR_REVIEW_DENY_REASON = (
    "BLOCKED: raw `glab api`/`gh api` POST to a merge-request/pull-request "
    "discussion/notes/comments endpoint bypasses the sanctioned review-post CLI. "
    "To CREATE a note use `t3 review post-comment <repo> <mr> --body-file <abspath>` "
    "(top-level `t3 review`, never overlay-scoped; draft by default, #1207) or "
    "`post-draft-note`; to EDIT use `t3 review update-note`; to REMOVE use "
    "`t3 review delete-discussion` — the CLI enforces draft-default, dedup, and "
    "on-behalf approval, which a direct REST write skips. Read-only GETs are unaffected."
)

ISSUE_NOTE_DENY_REASON = (
    "BLOCKED: raw `glab api`/`gh api` POST to an issue/work-item notes endpoint bypasses "
    "the sanctioned issue-note CLI. To CREATE a note use "
    "`t3 <overlay> ticket comment <issue-url> --body '<text>'` (or `--body-file <path>`); "
    "to REMOVE use `t3 review delete-issue-note <repo> <issue-iid> <note-id>` — "
    "the CLI routes the body through the public-repo leak gate and the send-proxy "
    "audit/allowlist/redaction seam, which a direct REST write skips. "
    "`t3 review post-comment` takes an integer MR IID and cannot address an "
    "issue; the forge exposes no draft-note API for issues, so there is no draft path here. "
    "Read-only GETs are unaffected."
)


def is_raw_review_write(command: str) -> bool:
    """Whether *command* is a raw forge REST WRITE to a review-comment endpoint.

    True only when the command targets a ``.../discussions``/``.../notes``/
    ``.../comments`` endpoint AND its effective HTTP method is not GET.
    """
    if not command or not _GLAB_GH_API_RE.search(command):
        return False
    if not REVIEW_POST_ENDPOINT_RE.search(command):
        return False
    methods = [m.upper() for pair in _METHOD_FLAG_RE.findall(command) for m in pair if m]
    if methods:
        is_read = methods[-1] == "GET"
    elif _BODY_FLAG_RE.search(command):
        is_read = False
    else:
        is_read = True
    return not is_read


def raw_review_deny_reason(command: str) -> str | None:
    """Return the deny reason for a raw review-post write, or ``None`` when allowed.

    The reason names the remedy for the surface *command* actually addressed. An
    issue/work-item note has no counterpart in the ``t3 review`` create seam — that
    seam is merge-request-shaped (``post-comment`` takes an integer MR IID, and its
    draft-default rests on a draft-notes API the forge exposes only under
    ``merge_requests``) — so its remedy is ``t3 <overlay> ticket comment``, which
    reaches the same issue-notes endpoint through the shared scanned forge-write seam.
    """
    if not is_raw_review_write(command):
        return None
    if ISSUE_NOTE_ENDPOINT_RE.search(command):
        return ISSUE_NOTE_DENY_REASON
    return MR_REVIEW_DENY_REASON


__all__ = [
    "ISSUE_NOTE_DENY_REASON",
    "MR_REVIEW_DENY_REASON",
    "is_raw_review_write",
    "raw_review_deny_reason",
]
