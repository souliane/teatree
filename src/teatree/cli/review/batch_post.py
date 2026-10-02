"""N review comments on one MR in a single publish envelope.

``/t3:review`` mandates one inline comment per finding, so a review is inherently a
BATCH — and posting it one comment at a time made each finding pay the full ceremony:
its own live authorization, its own on-behalf consume, and (from the CLI) two ~32s
process starts. Nothing about that ceremony is per-comment; it is per-REVIEW.

What stays per-comment is the only thing that must: the pre-publish gates. Every body
is scanned on its own (shape, bloat, general-note, TODO-anchor, evidence) and the whole
batch is refused naming the offending one, so batching can never launder a body past a
gate the single-comment path would have caught.

Extracted from :mod:`teatree.cli.review.service` for the same reason as
:mod:`teatree.cli.review.post_impl` — the service class holds the surface, the module
holds the mechanics.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace as _replace
from typing import TYPE_CHECKING

from teatree.cli.review.on_behalf import publish_or_blocked
from teatree.cli.review.send_routing import route_forge_send

if TYPE_CHECKING:
    from teatree.cli.review.evidence_gate import FindingEvidence
    from teatree.cli.review.service import ReviewService


@dataclass(frozen=True, slots=True)
class InlineNote:
    """One review finding: its body, the added diff line it anchors to (blank = MR-wide), its gate inputs.

    ``evidence`` and the two #126 escapes ride WITH the finding rather than being
    fixed for the batch, because every gate they feed is per-body: one finding needs
    the #1280 evidence receipts and its neighbour does not. Hardcoding them away here
    left the batch unable to post the "X is wrong/broken" class at all — the class a
    review most needs — while the single-comment path could.
    """

    note: str
    file: str = ""
    line: int = 0
    evidence: "FindingEvidence | None" = None
    force_general: bool = False
    allow_bloat: bool = False


def post_comments(
    service: "ReviewService", repo: str, mr: int, notes: Sequence[InlineNote], *, live: bool = False
) -> tuple[str, int]:
    """Post *notes* on ``repo!mr`` — every body gated, one authorization, one envelope.

    ``live=False`` is the colleague-INVISIBLE draft batch, which needs no authorization
    at all and simply routes each note through :meth:`ReviewService.post_comment`.

    ``live=True`` resolves the live authorization ONCE
    (:class:`~teatree.cli.review.authorize.LiveAuthorization`), runs the pre-publish
    gates on every body, and only then opens one
    :func:`~teatree.cli.review.on_behalf.publish_or_blocked` envelope around all of
    them — so one recorded approval covers the review instead of one per finding.

    A comment that fails mid-batch stops the run and is reported with the count that
    LANDED, because the forge posts are not transactional and cannot be unwound. The
    approval consume IS rolled back by the enclosing envelope, which is the safe
    direction: the operator's single authorization did not fully deliver, so it
    survives for a retry of the remainder.
    """
    if not notes:
        return "Refusing: no comments to post.", 1
    if not live:
        return _post_each_draft(service, repo, mr, notes)

    from teatree.cli.review.authorize import resolve_live_authorization  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.cli.review.default_draft import publish_live_post  # noqa: PLC0415 — lazy: monkeypatchable + ORM

    authorization = resolve_live_authorization(scope=f"{repo}!{mr}", action="post_comment")
    if authorization.refusal:
        return authorization.refusal, 1

    routed, refusal = _gate_every_body(service, repo, mr, notes)
    if refusal:
        return refusal, 1

    def publish() -> tuple[str, int]:
        return _publish_all(service, repo, mr, routed)

    return publish_or_blocked(
        repo,
        mr,
        "post_comment",
        lambda: publish_live_post(repo=repo, mr=mr, publish=publish, token_required=authorization.token_required),
    )


def _post_each_draft(service: "ReviewService", repo: str, mr: int, notes: Sequence[InlineNote]) -> tuple[str, int]:
    """Route each note through the ungated draft path, stopping at the first refusal."""
    landed = 0
    for index, item in enumerate(notes, start=1):
        message, code = service.post_comment(
            repo,
            mr,
            item.note,
            file=item.file,
            line=item.line,
            evidence=item.evidence,
            force_general=item.force_general,
            allow_bloat=item.allow_bloat,
        )
        if code:
            return f"{_landed_prefix(landed, len(notes))} comment {index}: {message}", 1
        landed += 1
    return f"OK posted {landed} draft comment(s) on {repo}!{mr}", 0


def _gate_every_body(
    service: "ReviewService", repo: str, mr: int, notes: Sequence[InlineNote]
) -> tuple[list[InlineNote], str]:
    """Run the pre-publish gates and the send-proxy on every body BEFORE anything publishes."""
    routed: list[InlineNote] = []
    for index, item in enumerate(notes, start=1):
        refusal = service._run_pre_publish_gates(  # noqa: SLF001 — the mechanics module owns the service's gate chain, as post_impl does
            repo=repo,
            mr=mr,
            note=item.note,
            file=item.file,
            line=item.line,
            action="post_comment",
            evidence=item.evidence,
            force_general=item.force_general,
            allow_bloat=item.allow_bloat,
        )
        if refusal:
            return [], f"Refusing the batch — comment {index}: {refusal}"
        body, send_refusal = route_forge_send(repo=repo, mr=mr, action="post_comment", note=item.note)
        if send_refusal:
            return [], f"Refusing the batch — comment {index}: {send_refusal}"
        routed.append(_replace(item, note=body))
    return routed, ""


def _publish_all(service: "ReviewService", repo: str, mr: int, notes: Sequence[InlineNote]) -> tuple[str, int]:
    """Post every gated body, stopping at the first failure and reporting what landed.

    A failure that lands NOTHING is an ordinary refusal and returns ``(message, 1)``:
    the enclosing envelope rolls the approval consume and the audit back together,
    which is right when there is nothing to have audited.

    A failure that lands SOMETHING raises instead, because those comments are
    permanently readable by colleagues under the user's identity. The raise still
    rolls the approval back (it did not fully deliver, so it survives the retry)
    while telling the envelope an audit is owed for what landed — rolling that back
    too would erase the only record of a published on-behalf post.

    ``BaseException`` and not ``Exception``: a Ctrl-C or a ``SystemExit`` after a
    comment has gone out leaves exactly the colleague-visible post the audit exists
    to record. The envelope re-raises the original cause, so the interrupt still
    terminates.
    """
    from teatree.core.on_behalf_gate_recorded import (  # noqa: PLC0415 — lazy: ORM-adjacent core import
        OnBehalfPartialPublishError,
    )

    landed = 0
    for item in notes:
        try:
            message, code = service._post_comment_impl(  # noqa: SLF001 — same service-mechanics seam as post_impl
                repo, mr, item.note, file=item.file, line=item.line
            )
        except BaseException as err:
            if not landed:
                raise
            reported = f"{_landed_prefix(landed, len(notes))} {err}", 1
            raise OnBehalfPartialPublishError(reported) from err
        if code:
            reported = f"{_landed_prefix(landed, len(notes))} {message}", 1
            if landed:
                raise OnBehalfPartialPublishError(reported)
            return reported
        landed += 1
    return f"OK posted {landed} comment(s) on {repo}!{mr}", 0


def _landed_prefix(landed: int, total: int) -> str:
    """The count that landed, plus the exact slice a retry still owes.

    The batch has no idempotency key, so a caller retrying the whole list re-posts what
    already went out. Naming the remaining slice is what the refusal can say for free —
    a durable record keyed on the comment would be the general fix.
    """
    if not landed:
        return f"Posted 0 of {total};"
    return f"Posted {landed} of {total} — retry with comments[{landed}:];"
