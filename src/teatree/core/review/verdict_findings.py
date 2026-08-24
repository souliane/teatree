"""Render a :class:`~teatree.core.models.review_verdict.ReviewVerdict`'s findings (#4476).

A HOLD's findings were persisted on the verdict and counted by ``review
status`` — and rendered by nothing, so ``findings_count: 4`` stood in front of
content no author, reviewer or operator could read without opening the
database. Worse, the count and the content could disagree: ``review status``
counted the RAW ``findings`` JSON rows while
:attr:`~teatree.core.models.review_verdict.ReviewVerdict.structured_findings`
silently dropped every non-dict one.

:func:`findings_payload` is the strict read that closes both gaps: it refuses an
unrenderable payload rather than dropping it, so a count is always backed by
content that renders. The two renderers sit on top of it — one for the CLI, one
for the PR comment the author actually reads.
"""

from collections.abc import Mapping
from typing import cast

from teatree.core.models.review_verdict import Finding, FindingDict, ReviewVerdict

MARKER_PREFIX = "<!-- teatree-review-verdict:"
"""Prefix of the hidden marker every published findings comment carries.

Dedup reads it back: a re-publish looks for the verdict's own marker among the
PR's comments and skips when one already matches, so recording the same verdict
twice never posts a second copy.
"""


class FindingsRenderError(ValueError):
    """A persisted findings payload cannot be rendered — loud, never a silent drop."""


def marker_for(verdict: ReviewVerdict) -> str:
    """The dedup marker identifying *verdict*'s published findings comment."""
    return f"{MARKER_PREFIX} pk={verdict.pk} sha={verdict.reviewed_sha} -->"


def findings_payload(verdict: ReviewVerdict) -> list[FindingDict]:
    """*verdict*'s findings as serialisable dicts, refusing an unrenderable row.

    The strict sibling of ``structured_findings``, which drops a malformed row
    and leaves the count overstating what can be read.
    """
    rows = verdict.findings
    if not isinstance(rows, list):
        msg = f"verdict {verdict.pk} findings is {type(rows).__name__}, not a list — nothing can be rendered from it"
        raise FindingsRenderError(msg)
    return [_renderable(verdict, index, raw).as_dict() for index, raw in enumerate(rows)]


def _renderable(verdict: ReviewVerdict, index: int, raw: object) -> Finding:
    if not isinstance(raw, dict):
        msg = (
            f"verdict {verdict.pk} finding {index} is {type(raw).__name__}, not an object — "
            f"the recorded findings_count cannot be backed by readable content"
        )
        raise FindingsRenderError(msg)
    finding = Finding.from_dict(raw)
    if not finding.summary.strip():
        msg = f"verdict {verdict.pk} finding {index} has an empty summary — it would render as a blank line"
        raise FindingsRenderError(msg)
    return finding


def render_findings_text(verdict: ReviewVerdict) -> str:
    """*verdict*'s findings as the human CLI view, one ``[severity] location — summary`` per line."""
    payload = findings_payload(verdict)
    if not payload:
        return f"  no findings recorded on {verdict.verdict} verdict {verdict.pk} ({verdict.slug}#{verdict.pr_id})"
    header = (
        f"  {verdict.verdict} verdict {verdict.pk} for {verdict.slug}#{verdict.pr_id}"
        f"@{verdict.reviewed_sha[:8]} — {len(payload)} finding(s), reviewer={verdict.reviewer_identity}"
    )
    return "\n".join([header, *(f"  {_line(row)}" for row in payload)])


def render_findings_markdown(verdict: ReviewVerdict) -> str:
    """*verdict*'s findings as the PR-comment body, carrying the dedup marker."""
    payload = findings_payload(verdict)
    if not payload:
        msg = f"verdict {verdict.pk} has no findings — there is nothing to publish"
        raise FindingsRenderError(msg)
    bullets = [f"- **[{row['severity']}]** `{_location(row)}` — {row['summary']}" for row in payload]
    return "\n".join(
        [
            f"### Cold review: {verdict.verdict} — {len(payload)} finding(s) @ `{verdict.reviewed_sha[:8]}`",
            "",
            *bullets,
            "",
            (
                f"Reviewer: `{verdict.reviewer_identity}`. Read them again with "
                f"`t3 <overlay> review findings <pr-url>`; a later merge_safe verdict at this head clears the hold."
            ),
            "",
            marker_for(verdict),
        ]
    )


def comment_carries_marker(comment: object, marker: str) -> bool:
    """Whether a forge comment payload already carries *marker*."""
    if not isinstance(comment, Mapping):
        return False
    body = cast("Mapping[str, object]", comment).get("body")
    return marker in str(body or "")


def _location(row: FindingDict) -> str:
    return Finding.from_dict(dict(row)).location()


def _line(row: FindingDict) -> str:
    return f"[{row['severity']}] {_location(row)} — {row['summary']}"
