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

from collections.abc import Callable
from typing import Protocol


class ReviewPostSeam(Protocol):
    def post_draft_note(self, repo: str, mr: int, note: str) -> tuple[str, int]: ...

    def post_comment(self, repo: str, mr: int, note: str, *, live: bool = False) -> tuple[str, int]: ...


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
