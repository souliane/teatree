"""Detect a raw ``glab api``/``gh api`` WRITE to a review-comment endpoint.

The pure matcher behind the PreToolUse raw-review-post gate (#1164 / #2384 PR6),
carried in a :mod:`teatree.hooks` leaf so BOTH the cold PreToolUse subprocess (via
``hooks/scripts/raw_review_post_guard.py``) AND Lane B's shared hard-deny registry
refuse the SAME set — a raw REST WRITE to a ``.../discussions``/``.../notes``/
``.../comments`` endpoint, which bypasses the sanctioned ``t3 <overlay> review
post-comment`` path (draft-default, dedup, on-behalf approval). A plain GET read
is allowed.

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
_GLAB_GH_API_RE = re.compile(r"\b(?:glab|gh)\s+api\b")
_METHOD_FLAG_RE = re.compile(r"(?:-X|--method)[\s=]+['\"]?([A-Za-z]+)\b|(?<=-X)([A-Za-z]+)\b")
_BODY_FLAG_RE = re.compile(r"(?:^|\s)(?:-f|--field|-F|--raw-field|--input|-d|--data)\b")

_RAW_REVIEW_DENY_REASON = (
    "BLOCKED: raw `glab api`/`gh api` POST to a review discussion/notes/comments "
    "endpoint bypasses the sanctioned review-post CLI. To CREATE a note use "
    "`t3 <overlay> review post-comment` (draft by default, #1207) or `post-draft-note`; "
    "to EDIT use `t3 <overlay> review update-note`; to REMOVE use `delete-discussion` (MR) "
    "or `delete-issue-note` (issue/work-item) — the CLI enforces draft-default, dedup, and "
    "on-behalf approval, which a direct REST write skips. Read-only GETs are unaffected."
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
    """Return the deny reason for a raw review-post write, or ``None`` when allowed."""
    if not is_raw_review_write(command):
        return None
    return _RAW_REVIEW_DENY_REASON


__all__ = ["is_raw_review_write", "raw_review_deny_reason"]
