"""Review-post seam for the MCP write tools (#3076).

``t3 review post-comment`` / ``post-draft-note`` live in ``teatree.cli.review``
— ABOVE ``teatree.mcp`` in the layer graph — so, exactly like
:mod:`teatree.mcp.command_catalogue`, the dependency is INVERTED:
``teatree.cli`` registers a factory at import time via
:func:`register_review_post_seam`, and the MCP write tools reach the gated
review service (live-post approval #1207, on-behalf verdict, shape / bloat /
banned-terms pre-publish gates) only through it. The default factory raises
loud, so a caller that never registered one fails with a clear message rather
than silently bypassing the gate-carrying seam.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

#: An inline anchor — ``(file, line)`` on an added line of the MR diff; ``None`` posts a general note.
type InlineAnchor = tuple[str, int]


@dataclass(frozen=True, slots=True)
class SeamNote:
    """One review finding crossing the seam — its body, its anchor, and its gate inputs.

    The same record serves the single-post and the batch surfaces, so a finding
    carries the identical set either way and the batch is literally N of these.

    ``evidence_json`` crosses as the very JSON text ``t3 review post-comment
    --evidence-json`` takes: :class:`~teatree.cli.review.evidence_gate.FindingEvidence`
    lives ABOVE this layer, so the seam carries the text and
    :func:`~teatree.cli.review.evidence_gate.FindingEvidence.from_json` stays the one
    parser. Without it the #1280 evidence gate refuses every "wrong/broken" finding —
    the class a review most needs to post.

    ``force_general`` / ``allow_bloat`` are the #126 per-call escapes for the
    multi-finding general-note and comment-bloat gates; both default to the value
    that changes nothing, so an unset one never widens a gate.
    """

    note: str
    anchor: InlineAnchor | None = None
    evidence_json: str = ""
    force_general: bool = False
    allow_bloat: bool = False


class ReviewPostSeam(Protocol):
    def post_draft_note(self, repo: str, mr: int, note: SeamNote) -> tuple[str, int]: ...

    def post_comment(self, repo: str, mr: int, note: SeamNote, *, live: bool = False) -> tuple[str, int]: ...

    def post_comments(
        self, repo: str, mr: int, notes: Sequence[SeamNote], *, live: bool = False
    ) -> tuple[str, int]: ...


SeamFactory = Callable[[str], ReviewPostSeam]


def _unregistered_factory(_repo: str) -> ReviewPostSeam:
    msg = "review-post seam not registered — teatree.cli must call register_review_post_seam() at import time"
    raise RuntimeError(msg)


_factory: SeamFactory = _unregistered_factory


def register_review_post_seam(factory: SeamFactory) -> None:
    """Inject the gated review-service factory (called by ``teatree.cli`` at import time)."""
    global _factory  # noqa: PLW0603 — the single registration seam for the inverted dependency
    _factory = factory


def review_post_seam(repo: str) -> ReviewPostSeam:
    """The gated review poster for *repo*, via the registered factory.

    *repo* is the ``owner/name`` slug the tool was called with: the service resolves
    its forge base URL and API token from the overlay that owns it, so the target is
    derived from the call rather than from whichever overlay is ambient (#3793).
    """
    return _factory(repo)
