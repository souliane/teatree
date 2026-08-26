"""Deny a raw ``glab api``/``gh api`` WRITE to a review-comment endpoint (#2384 PR6).

Sub-agents have repeatedly posted MR/PR review comments by shelling out to a raw
forge REST POST — ``glab api projects/.../merge_requests/<n>/discussions -X POST``
(or ``.../notes``, or the GitHub ``.../pulls/<n>/comments``) — bypassing the
sanctioned top-level ``t3 review post-comment`` / ``post-draft-note`` path that
enforces draft-default (#1207), dedup, and on-behalf approval (#960). RED-CARD,
5x recurrence. This gate closes the bypass at the Bash boundary: a WRITE to a
review discussion/notes/comments endpoint is denied; plain GET reads pass through.

The refusal names the CLI that can address the surface the caller actually hit.
An ISSUE/work-item note has no counterpart in the ``t3 review`` create seam —
``post-comment`` takes an integer MR IID, and its draft-default rests on a
draft-notes API the forge exposes only under ``merge_requests`` — so its remedy is
``t3 <overlay> ticket comment <issue-url>``, which reaches the same issue-notes
endpoint through the shared scanned forge-write seam (the public-repo leak gate +
the #117 send-proxy audit/allowlist/redaction that ``chokepoints.yaml`` mandates
for every ``post_issue_comment``). A single remedy for both surfaces blocked the
caller and then pointed at a command that could not do the job.

Conservative by construction: it matches ONLY the review-comment endpoints
(discussions / notes / comments) and classifies the command by its EFFECTIVE HTTP
method — the one gh (2.87.3) / glab (1.80.4) actually send. Both CLIs resolve
repeated ``-X``/``--method`` flags LAST-WINS (``-X GET -X POST`` POSTs), and with
NO method flag they default to POST if a request-body/field flag is present, else
GET. A command is a READ iff its effective method is GET — only then does the
forge send ``-f`` as a query parameter rather than a body write (#1568). Every
other effective method (POST/PUT/PATCH/DELETE/…) is a write. Fails OPEN on an
internal parse error — a gate bug must never wedge the fleet.

Extracted whole from ``hook_router`` (the #2384 Wave-2 router split, PR6) so the
dispatcher shrinks; the router re-exports :func:`handle_block_raw_review_post`
into ``_HANDLERS`` unchanged. The deny routes through the router's shared
``emit_pretooluse_deny`` chokepoint (back-imported lazily), so the
``_write_pretooluse_deny`` deny writer and the repeated-denial circuit breaker
stay in the router. A narrow targeted-command gate — it denies only a raw
review-post, never arbitrary Bash — so it is on the never-lockout allowlist.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib —
never Django / ``teatree.core``. The effective-HTTP-method regexes
(``_GLAB_GH_API_RE`` / ``_REVIEW_POST_METHOD_RE`` / ``_REVIEW_POST_BODY_FLAG_RE``)
are SHARED with handlers in the router (``_effective_method_is_write`` and the
out-of-band-merge gate), so they live in the ``forge_api_detect`` sibling — one
canonical definition each — and are back-imported lazily inside the handler body.
Only ``REVIEW_POST_ENDPOINT_RE`` and the deny reason, used solely by this gate, live here.
"""

import re
import sys

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("raw_review_post_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.raw_review_post_guard", sys.modules[__name__])

REVIEW_POST_ENDPOINT_RE = re.compile(
    r"(?:merge_requests|pulls|issues)/\d+/(?:discussions|notes|comments)\b",
)
ISSUE_NOTE_ENDPOINT_RE = re.compile(r"issues/\d+/(?:notes|comments)\b")
_MR_REVIEW_DENY_REASON = (
    "BLOCKED: raw `glab api`/`gh api` POST to a merge-request/pull-request "
    "discussion/notes/comments endpoint bypasses the sanctioned review-post CLI. "
    "To CREATE a note use `t3 review post-comment <repo> <mr> --body-file <abspath>` "
    "(top-level `t3 review`, never overlay-scoped; draft by default, #1207) or "
    "`post-draft-note`; to EDIT use `t3 review update-note`; to REMOVE use "
    "`t3 review delete-discussion` — the CLI enforces draft-default, dedup, and "
    "on-behalf approval, which a direct REST write skips. Read-only GETs are unaffected."
)
_ISSUE_NOTE_DENY_REASON = (
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

    True only when the command targets a ``.../discussions``, ``.../notes``, or
    ``.../comments`` endpoint AND its EFFECTIVE HTTP method is not GET. The
    effective method models gh/glab semantics: the LAST ``-X``/``--method`` value
    wins (so ``-X GET -X POST`` is a POST write, ``-X POST -X GET`` is a GET read);
    with no method flag the forge defaults to POST when a body/field flag is
    present, else GET. A forced GET sends body flags as query params and cannot
    create a comment, so it is the only read (#1568).

    Uses a word-boundary regex (not plain ``in``) so ``glab  api`` / ``gh  api``
    double-space variants are caught (F4). The effective-method regexes live in
    the ``forge_api_detect`` sibling (shared with ``_effective_method_is_write``
    and the out-of-band-merge gate) and are back-imported lazily — one definition each.
    """
    from hooks.scripts.forge_api_detect import (  # noqa: PLC0415 deferred back-import
        _GLAB_GH_API_RE,
        _REVIEW_POST_BODY_FLAG_RE,
        _REVIEW_POST_METHOD_RE,
    )

    if not _GLAB_GH_API_RE.search(command):
        return False
    if not REVIEW_POST_ENDPOINT_RE.search(command):
        return False
    methods = [m.upper() for pair in _REVIEW_POST_METHOD_RE.findall(command) for m in pair if m]
    if methods:
        is_read = methods[-1] == "GET"
    elif _REVIEW_POST_BODY_FLAG_RE.search(command):
        is_read = False
    else:
        is_read = True
    return not is_read


def review_post_deny_reason(command: str) -> str | None:
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
        return _ISSUE_NOTE_DENY_REASON
    return _MR_REVIEW_DENY_REASON


def handle_block_raw_review_post(data: dict) -> bool:
    """Deny a raw ``glab api``/``gh api`` WRITE to a review-comment endpoint.

    Forces the sanctioned top-level ``t3 review post-comment`` / ``post-draft-note``
    path (draft-default + dedup + on-behalf approval), which a direct REST write
    skips. Conservative: a command is denied only when its effective HTTP method
    (last ``-X``/``--method`` wins; default POST when a body flag is present) is
    not GET. Reads — bare, explicit-GET, or write-then-GET — and non-review
    endpoints pass through. Returns True when a deny was emitted (caller stops the
    handler chain).

    The deny routes through the router's shared ``emit_pretooluse_deny`` chokepoint
    (back-imported lazily; the ``_write_pretooluse_deny`` writer + circuit breaker
    stay in the router).
    """
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    reason = review_post_deny_reason(command) if command else None
    if reason is None:
        return False
    return emit_pretooluse_deny(reason)
