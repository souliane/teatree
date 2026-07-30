"""Review-shape gate: stay inline once inline (PR-08, folds #1212).

The discipline this enforces: once a reviewer has anchored one or more findings
inline on an MR, the *rest* of that review stays inline too — a subsequent
MR-level (general) draft note fragments a review that was already speaking in
file:line terms, which is exactly the shape the #72 discipline discourages. The
sibling :mod:`teatree.cli.review.general_inline_gate` catches a *single* general
note that crams multiple findings; this gate catches the *cross-note* case: a
general note posted while inline drafts already exist on the same MR.

The gate runs only on the **general** path (no ``file``+``line`` anchor) and
refuses the post — before any GitLab publish — when the MR already carries at
least one inline draft note (a draft with a diff ``position``). It fetches the
MR's existing draft notes through the same endpoint
:meth:`ReviewService.list_draft_notes` reads.

Fail-open on a read failure: a network/auth error fetching the drafts returns
``""`` (proceed) — the gate must never wedge a legitimate post on a flaky GET.
``force_general`` is the documented per-call escape (surfaced as
``--force-general``, shared with the sibling gate) for a genuinely MR-wide note.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from teatree.cli.review.guarded_read import guarded_read

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabHTTPClient


def _is_inline_draft(note: object) -> bool:
    """Whether a draft-notes entry is anchored inline (has a diff position)."""
    if not isinstance(note, Mapping):
        return False
    position = cast("Mapping[str, object]", note).get("position")
    if not isinstance(position, Mapping):
        return False
    # An inline anchor carries a concrete new/old line; an empty position dict
    # is not an inline anchor.
    anchor = cast("Mapping[str, object]", position)
    return any(anchor.get(key) is not None for key in ("new_line", "old_line", "new_path", "old_path"))


def count_inline_drafts(api: "GitLabHTTPClient", encoded_repo: str, mr: int) -> int:
    """Return the number of existing inline draft notes on the MR.

    Best-effort: a failed fetch (missing token, network, a test stub without the
    endpoint) returns 0, so the gate fails open rather than refusing every draft
    note whenever the forge is unreachable. The read goes through
    :func:`~teatree.cli.review.guarded_read.guarded_read`, so that failure is
    logged instead of being indistinguishable from "no drafts pending" (#3509).
    """
    endpoint = f"projects/{encoded_repo}/merge_requests/{mr}/draft_notes"
    outcome = guarded_read(f"the inline draft notes on MR !{mr}", lambda: api.get_json(endpoint), neutral=None)
    if outcome.failed:
        return 0
    notes = outcome.value
    if not isinstance(notes, list):
        return 0
    return sum(1 for note in notes if _is_inline_draft(note))


def check_inline_shape(
    *,
    api: "GitLabHTTPClient",
    encoded_repo: str,
    mr: int,
    inline: bool,
    force_general: bool = False,
) -> str:
    """Return a non-empty refusal when an MR-level note is posted with inline drafts pending.

    Returns ``""`` (proceed) when any of these hold:

    * ``force_general`` is set — the documented per-call escape for a genuinely
        MR-wide note, OR
    * ``inline`` is true — the post IS being anchored inline, so it is not the
        MR-level fragmentation this gate targets, OR
    * the MR has no existing inline draft notes.

    Otherwise returns a refusal naming the count of pending inline drafts and
    steering the reviewer to keep this finding inline too.
    """
    if force_general or inline:
        return ""
    pending = count_inline_drafts(api, encoded_repo, mr)
    if pending == 0:
        return ""
    return (
        f"Refusing MR-level draft note: this MR already has {pending} inline draft note(s), so stay "
        f"inline — post this finding on its own file:line too, don't fragment an inline review with an "
        f"MR-level note:\n"
        '  t3 review post-draft-note <repo> <mr> "<note>" --file <path> --line <n>\n'
        "Pass --force-general to override ONLY for a genuinely MR-wide note (a verdict-only summary with "
        "no per-line finding)."
    )
