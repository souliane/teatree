"""The MCP review-post write tools — the anchored/batched surface over the gated seam.

Split out of :mod:`teatree.mcp.write_tools` (module-health LOC ceiling): these
share one concern — turning an MCP call's finding payload into the seam's
:class:`~teatree.mcp.review_seam.SeamNote` and refusing a malformed one before any
network call. Registration stays in ``write_tools`` beside every other tool, exactly
as the handler bodies for the command-shaped writes do.

All three tools take the SAME finding shape — ``{note, anchor, evidence,
force_general, allow_bloat}`` — so the single-post tools are the batch at arity one
and there is one thing to learn rather than three. It is a mapping rather than a
spread of keyword arguments because the fields belong to the FINDING, not to the
call: the batch has always carried them per entry, and a review posts N findings.

``evidence`` / ``force_general`` / ``allow_bloat`` are the same per-call gate inputs
the CLI carries as ``--evidence-json`` / ``--force-general`` / ``--allow-bloat``.
They are on this surface because their gates run here too: without ``evidence`` the
#1280 gate refuses every "X is wrong / broken / missing" body, which is most of what
a real review has to say, and the finding would have to go back to the CLI these
tools exist to replace.
"""

import json
from collections.abc import Mapping
from typing import Any

from asgiref.sync import sync_to_async

from teatree.mcp.review_seam import InlineAnchor, SeamNote, review_post_seam

_ANCHOR_REFUSAL = (
    "Refusing: anchor must be 'path/to/file.py:LINE' (an added line of the MR diff). "
    "Omit it to post a general (MR-wide) note."
)
_BAD_INPUT = 2
_EVIDENCE_REFUSAL = "Refusing: 'evidence' must be a JSON object or its JSON string, got {kind}."
# Distinct wording from `_EVIDENCE_REFUSAL`: the shape was right, so telling the caller the
# type is wrong sends them to fix the one thing that is not.
_UNSERIALISABLE_EVIDENCE_REFUSAL = (
    "Refusing: 'evidence' is a mapping json cannot serialise — {error}. "
    "Use JSON-native values only (string, number, boolean, null, array, object)."
)


def _parse_anchor(anchor: str) -> InlineAnchor | str | None:
    """``"path:line"`` → ``(path, line)``; blank → ``None`` (general note); malformed → the refusal text."""
    if not anchor.strip():
        return None
    path, _, line = anchor.strip().rpartition(":")
    # ASCII digits only. ``str.isdigit`` also admits Unicode digit forms no diff line
    # number is ever written in, and ``int()`` splits them two ways: "²" RAISES out of
    # the tool handler instead of returning this refusal, and "٤" parses quietly as 4 —
    # an anchor landing on a line the caller never named.
    if not path or not line.isascii() or not line.isdigit() or int(line) < 1:
        return _ANCHOR_REFUSAL
    return path, int(line)


def _evidence_json(raw: object) -> str | None:
    """The #1280 record as JSON text; ``None`` when *raw* is not a record at all.

    ``finding`` is itself a mapping whose other values are real objects, so a mapping is
    the shape a model naturally passes for ``evidence`` too. ``str()`` on one yields a
    Python repr with single quotes, which ``FindingEvidence.from_json`` rejects — so the
    finding class the batch exists to post could not be posted in its natural shape.
    Anything that is neither a mapping nor a string is REFUSED rather than stringified:
    accepting a mapping must not widen into accepting anything. A mapping json cannot
    serialise raises here; mapping that to a refusal is :func:`_seam_note`'s job, since it
    is the one function that already turns a bad payload into refusal text.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        return json.dumps(raw)
    return None


def _seam_note(finding: dict[str, Any]) -> SeamNote | str:
    """Build the seam's record from one finding payload, or the refusal it earned."""
    note = str(finding.get("note", "")).strip()
    if not note:
        return "Refusing: the finding carries no 'note'."
    parsed = _parse_anchor(str(finding.get("anchor", "")))
    if isinstance(parsed, str):
        return parsed
    raw_evidence = finding.get("evidence")
    try:
        evidence_json = _evidence_json(raw_evidence)
    except (TypeError, ValueError) as err:
        return _UNSERIALISABLE_EVIDENCE_REFUSAL.format(error=err)
    if evidence_json is None:
        return _EVIDENCE_REFUSAL.format(kind=type(raw_evidence).__name__)
    return SeamNote(
        note=note,
        anchor=parsed,
        evidence_json=evidence_json,
        force_general=bool(finding.get("force_general")),
        allow_bloat=bool(finding.get("allow_bloat")),
    )


async def _review_post_draft_note(repo: str, mr: int, finding: dict[str, Any]) -> dict[str, Any]:
    """Post a colleague-INVISIBLE draft review note — inline when the finding names an anchor.

    ``finding`` is ``{"note": ..., "anchor": "path/to/file.py:LINE", "evidence": ...}``;
    a blank or absent ``anchor`` posts a general note, and ``evidence`` is the #1280
    record as JSON (the same object ``--evidence-json`` takes). Routes through the
    registered review seam — the exact ``t3 review post-draft-note`` service — so the
    anchor validation and the shape / bloat / evidence / banned-terms pre-publish gates
    apply identically to the CLI.
    """
    built = _seam_note(finding)
    if isinstance(built, str):
        return {"message": built, "code": _BAD_INPUT}
    message, code = await sync_to_async(
        lambda: review_post_seam(repo).post_draft_note(repo, mr, built),
        thread_sensitive=True,
    )()
    return {"message": message, "code": code}


async def _review_post_comment(repo: str, mr: int, finding: dict[str, Any], *, live: bool = False) -> dict[str, Any]:
    """Post one review comment — DRAFT by default, inline when the finding names an anchor.

    ``finding`` takes the same shape :func:`_review_post_comments` accepts per entry.
    Routes through the registered review seam — the exact ``t3 review post-comment``
    service — so ``live=true`` requires the recorded authorization (#1207) plus the
    on-behalf verdict, identically to the CLI. Without ``evidence`` a
    "X is wrong / broken / missing" body is refused; with it, it posts.
    """
    built = _seam_note(finding)
    if isinstance(built, str):
        return {"message": built, "code": _BAD_INPUT}
    message, code = await sync_to_async(
        lambda: review_post_seam(repo).post_comment(repo, mr, built, live=live),
        thread_sensitive=True,
    )()
    return {"message": message, "code": code}


async def _review_post_comments(
    repo: str, mr: int, comments: list[dict[str, Any]], *, live: bool = False
) -> dict[str, Any]:
    """Post a whole review's findings on one MR in a SINGLE gated batch.

    Each entry is one finding in the shape above. Every field is per-COMMENT because
    every gate it feeds is: one finding needs the #1280 evidence receipts and the nit
    beside it does not. Every body runs the same pre-publish gates as a single
    ``review_post_comment``; what the batch shares is the authorization — one recorded
    approval covers the review instead of one per finding. A malformed entry refuses
    the whole batch naming its position, before anything reaches the seam.
    """
    notes: list[SeamNote] = []
    for index, comment in enumerate(comments, start=1):
        built = _seam_note(comment)
        if isinstance(built, str):
            return {"message": f"Refusing: comment {index}: {built}", "code": _BAD_INPUT}
        notes.append(built)
    message, code = await sync_to_async(
        lambda: review_post_seam(repo).post_comments(repo, mr, notes, live=live),
        thread_sensitive=True,
    )()
    return {"message": message, "code": code}
