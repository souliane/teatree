"""Author-marked TODO/FIXME anchor gate (souliane/teatree#1186).

When a reviewer posts a blocker-shaped comment via the publishing methods
on :class:`teatree.cli.review.ReviewService` anchored to (or within ±3
lines of) an author-marked TODO/FIXME/XXX/HACK marker on an added line,
the gate refuses the post. The author has explicitly documented the work
is deferred via the marker; re-asking them to implement it makes the
reviewer look unable to read code (see #1186).

Sibling gates on the same publishing flow:

* :mod:`teatree.cli.review_on_behalf` — recorded-approval gate (the
    reviewer's identity is the user's; outbound posts need an approval row).
* :mod:`teatree.cli.review_shape_gate` — colleague-MR shape gate (single
    terse inline ``Nit:`` form).

This module is independent of both. It runs against every publishing call
that takes a body AND an inline anchor (``file`` and ``line`` both set).
General (MR-level) notes bypass the gate — they have no anchored author
intent to read.

Design choices:

* **Body-shape detection.** The CLI does not currently expose a
    ``--verdict REQUEST_CHANGES`` flag. Until it does, the gate detects
    blocker-shaped language directly in the body (``must``, ``blocking``,
    ``has to``, ``needs to``, ``required``, ``cannot merge``, etc.). The
    list intentionally errs on the side of refusing — a false-positive
    on a borderline body is a harmless downgrade to "rephrase the
    comment"; a false-negative is the #1186 failure mode recurring.
* **Fail-open.** Any failure to fetch the diff (network, missing token,
    test stub without the relevant endpoints) returns ``""`` — the gate
    is an additional safety net, never a hard block on the existing
    happy path.
* **Window size ±3.** The marker can sit slightly above or below the
    diff line the reviewer chose to anchor on. A 7-line window
    (anchor + 3 above + 3 below) covers the #1186 shape (anchor on the
    TODO itself) and the common "anchor on the function body, TODO on
    the same line or the function header" variant.
"""

import re
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabHTTPClient

# Mirrors :data:`teatree.cli.review_diff.ChangeEntry`: a GitLab change-entry
# dict in an MR /changes response. Object-typed values rather than narrow
# types because the API surface mixes strings (paths, diffs) and bools
# (renamed/new_file flags).
type ChangeEntry = dict[str, object]

TODO_ANCHOR_WINDOW = 3

_TODO_MARKER_RE = re.compile(
    r"(?:#|//|/\*|\*)\s*(?:TODO|FIXME|XXX|HACK)\b|"
    r"\b(?:not\s+in\s+this\s+(?:MR|PR)|follow[\s-]?up|deferred|"
    r"implement\s+later|out\s+of\s+scope)\b",
    re.IGNORECASE,
)

# Blocker-shaped language in the COMMENT BODY (not in the code). Matches
# the #1186 shape — "must be done", "this is blocking", "has to be
# implemented", "required before merge", etc. Anchored on word
# boundaries so an incidental "blockchain" or "mustard" does not trip.
_BLOCKER_BODY_RE = re.compile(
    r"\b(?:"
    r"must\s+(?:be|do|happen|fix|implement|add|remove|change)|"
    r"has\s+to\s+(?:be|do|happen|implement|fix)|"
    r"needs?\s+to\s+(?:be|do|happen|implement|fix)|"
    r"should\s+be\s+(?:done|fixed|implemented|addressed)\s+(?:before|in)\s+(?:merge|this)|"
    r"required\s+before\s+merge|"
    r"this\s+is\s+blocking|"
    r"blocking[:\s]|"
    r"cannot\s+merge|"
    r"can'?t\s+merge|"
    r"do\s+not\s+merge|"
    r"don'?t\s+merge"
    r")\b",
    re.IGNORECASE,
)

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def looks_like_blocker(body: str) -> bool:
    """Whether ``body`` reads as a blocker (REQUEST_CHANGES-shaped) comment.

    True when the body contains any of the canonical blocker phrases
    (``must``, ``blocking``, ``cannot merge``, ``required before merge``,
    etc.). The list is biased to refuse — a false-positive on a borderline
    body costs one rephrase; a false-negative recurs #1186.
    """
    if not body:
        return False
    return _BLOCKER_BODY_RE.search(body) is not None


def _collect_added_lines_with_text(diff_text: str) -> dict[int, str]:
    """Return ``{new_line_number: line_text}`` for every ``+``-added line.

    Mirrors :func:`teatree.cli.review_diff.find_added_line` but keeps the
    line text so we can scan for TODO markers without re-fetching.
    """
    added: dict[int, str] = {}
    nl: int | None = None
    for raw in diff_text.splitlines():
        m = _HUNK_HEADER.match(raw)
        if m:
            nl = int(m.group(1))
            continue
        if nl is None:
            continue
        sign = raw[:1] if raw else " "
        if sign == "-":
            continue
        if sign == "+":
            added[nl] = raw[1:]  # strip the leading '+'
        nl += 1
    return added


def _fetch_file_diff(api: "GitLabHTTPClient", encoded_repo: str, mr: int, file: str) -> str:
    """Return the unified diff for ``file`` in the MR, or ``""`` on any failure.

    Independent of :func:`teatree.cli.review_diff.fetch_file_diff` to keep
    the failure mode purely fail-open — any error path returns ``""`` and
    the gate proceeds to allow the post.
    """
    try:
        changes = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}/changes?access_raw_diffs=true")
    except Exception:  # noqa: BLE001 — fail-open on any network/auth failure
        return ""
    if not isinstance(changes, dict):
        return ""
    files_raw = changes.get("changes")
    if not isinstance(files_raw, list):
        return ""
    files = cast("list[ChangeEntry]", [f for f in files_raw if isinstance(f, dict)])
    for entry in files:
        if entry.get("new_path") == file or entry.get("old_path") == file:
            return str(entry.get("diff") or "")
    return ""


class InlineAnchor(NamedTuple):
    """The ``(file, line)`` pair an inline review note anchors on.

    Bundled as a single argument so the gate signature stays at five
    parameters — :pep:`8` and the project's ``PLR0913`` ceiling. ``file=""``
    or ``line=0`` means "general (MR-level) note, no anchor".
    """

    file: str
    line: int


def check_todo_anchor(  # noqa: PLR0913 — gate entry-point; each kwarg is a documented gate input (MR coordinate + body + anchor + the #126 override).
    *,
    api: "GitLabHTTPClient",
    encoded_repo: str,
    mr: int,
    body: str,
    anchor: InlineAnchor,
    allow_todo_blocker: bool = False,
) -> str:
    """Return a non-empty refusal when a blocker-shaped post anchors on a TODO marker.

    Returns ``""`` (proceed) when any of these hold:

    * ``allow_todo_blocker`` is set — the documented escape for the
        legitimately-authorized case where the in-MR blocker genuinely must
        be addressed despite the author's deferral marker (the CLI surfaces
        this as ``--allow-todo-blocker``, mirroring the sibling
        ``--quote-ok`` / ``--allow-banned-term`` overrides).
    * The anchor is empty (general MR-level note — nothing to anchor on,
        no author intent to read).
    * ``body`` does not look like a blocker.
    * The diff fetch fails (fail-open — never break the happy path).
    * No TODO/FIXME/XXX/HACK marker (or deferral phrase) sits on the
        anchor line or within :data:`TODO_ANCHOR_WINDOW` of it on the
        ``+``-added lines of the MR diff.

    Returns a clear refusal string otherwise. The caller short-circuits
    the GitLab API call with ``(message, 1)`` — same shape as the
    sibling gates.
    """
    if allow_todo_blocker:
        return ""
    if not anchor.file or not anchor.line:
        return ""
    if not looks_like_blocker(body):
        return ""
    diff_text = _fetch_file_diff(api, encoded_repo, mr, anchor.file)
    if not diff_text:
        return ""
    added = _collect_added_lines_with_text(diff_text)
    for offset in range(-TODO_ANCHOR_WINDOW, TODO_ANCHOR_WINDOW + 1):
        neighbour = anchor.line + offset
        text = added.get(neighbour)
        if text and _TODO_MARKER_RE.search(text):
            return _refusal(anchor.file, anchor.line, neighbour, text.strip())
    return ""


def _refusal(file: str, anchor_line: int, marker_line: int, marker_text: str) -> str:
    """Build the actionable refusal message.

    Names the file, the anchor line, the marker line, and the marker text
    so the agent knows exactly which TODO it tripped on. The remediation
    text steers toward "downgrade to a non-blocker comment or skip
    entirely" — never toward "post it anyway" — because re-asking the
    author to do work they have already deferred is the failure this gate
    exists to prevent.
    """
    truncated = marker_text if len(marker_text) <= 80 else marker_text[:77] + "..."  # noqa: PLR2004
    at_anchor = "at the anchor line" if marker_line == anchor_line else f"at line {marker_line} (±3 of anchor)"
    return (
        f"Refusing TODO-anchored blocker post: {file}:{anchor_line} — the diff line "
        f"{at_anchor} reads {truncated!r}, which is the AUTHOR explicitly documenting "
        "this work is deferred (not in this MR). Re-asking them to implement it makes "
        "the reviewer look unable to read code (see #1186).\n"
        "Remediation: downgrade to a non-blocker comment (e.g. `Nit: tracked at #NNN`), "
        "or skip the comment entirely. If you genuinely believe the TODO must be "
        "addressed in THIS MR, STOP and surface to the user — do NOT post on their "
        "identity."
    )
