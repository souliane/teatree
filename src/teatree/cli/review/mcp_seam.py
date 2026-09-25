"""Registers the gated review-post seam the MCP write tools consume (#3076).

``teatree.cli`` sits ABOVE ``teatree.mcp`` in the layer graph, so the
dependency is inverted (same shape as ``command_catalogue``): :func:`register`
(called explicitly from ``cli/__init__``) installs a factory that builds the
real gated :class:`~teatree.cli.review.service.ReviewService`, and the MCP write
tools reach it only through :mod:`teatree.mcp.review_seam`. The service carries
every publish gate (live-post approval #1207, on-behalf verdict, shape / bloat /
banned-terms scrub), so the MCP surface never bypasses them.

This module is also the ONLY place a :class:`~teatree.mcp.review_seam.SeamNote`
becomes the service's ``file=`` / ``line=`` / ``evidence=`` arguments. Everything
downstream reads THOSE — ``inline = bool(file and line)`` decides which branch the
general-note and inline-shape gates take — so a translation that drops what it was
handed does not fail loudly anywhere: it silently posts a review's findings as
MR-wide notes and lets a multi-finding body past a gate meant to refuse it.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from teatree.cli.review.batch_post import InlineNote
from teatree.cli.review.evidence_gate import FindingEvidence
from teatree.cli.review.service import ReviewService
from teatree.mcp.review_seam import SeamNote, register_review_post_seam

#: A caller-input refusal (malformed evidence JSON), distinct from a gate's ``1``.
_BAD_INPUT = 2


def _finding(note: SeamNote) -> tuple[InlineNote | None, str]:
    """The service-side finding for *note*, or the refusal its evidence JSON earned.

    ``FindingEvidence.from_json`` is the single parser for evidence text — the CLI's
    ``--evidence-json`` flag reaches it the same way — so the seam carries the text and
    never grows a second reading of the schema.
    """
    file, line = note.anchor or ("", 0)
    try:
        evidence = FindingEvidence.from_json(note.evidence_json) if note.evidence_json else None
    except ValueError as err:
        return None, str(err)
    return InlineNote(
        note=note.note,
        file=file,
        line=line,
        evidence=evidence,
        force_general=note.force_general,
        allow_bloat=note.allow_bloat,
    ), ""


@dataclass(frozen=True, slots=True)
class GatedReviewPoster:
    """The review-post seam over one gated :class:`ReviewService`."""

    service: ReviewService

    def post_draft_note(self, repo: str, mr: int, note: SeamNote) -> tuple[str, int]:
        finding, refusal = _finding(note)
        if finding is None:
            return refusal, _BAD_INPUT
        return self.service.post_draft_note(
            repo,
            mr,
            finding.note,
            file=finding.file,
            line=finding.line,
            evidence=finding.evidence,
            force_general=finding.force_general,
            allow_bloat=finding.allow_bloat,
        )

    def post_comment(self, repo: str, mr: int, note: SeamNote, *, live: bool = False) -> tuple[str, int]:
        finding, refusal = _finding(note)
        if finding is None:
            return refusal, _BAD_INPUT
        return self.service.post_comment(
            repo,
            mr,
            finding.note,
            file=finding.file,
            line=finding.line,
            live=live,
            evidence=finding.evidence,
            force_general=finding.force_general,
            allow_bloat=finding.allow_bloat,
        )

    def post_comments(self, repo: str, mr: int, notes: Sequence[SeamNote], *, live: bool = False) -> tuple[str, int]:
        findings: list[InlineNote] = []
        for index, note in enumerate(notes, start=1):
            finding, refusal = _finding(note)
            if finding is None:
                return f"Refusing the batch — comment {index}: {refusal}", _BAD_INPUT
            findings.append(finding)
        return self.service.post_comments(repo, mr, findings, live=live)


def _build_review_poster(repo: str) -> GatedReviewPoster:
    """Build the gated poster for *repo* — its forge target derives from that slug (#3793)."""
    return GatedReviewPoster(ReviewService(ReviewService.get_gitlab_token(repo), repo=repo))


def register() -> None:
    """Install the gated review-post seam factory into :mod:`teatree.mcp.review_seam`."""
    register_review_post_seam(_build_review_poster)
