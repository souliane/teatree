"""Registers the gated review-post seam the MCP write tools consume (#3076).

``teatree.cli`` sits ABOVE ``teatree.mcp`` in the layer graph, so the
dependency is inverted (same shape as ``command_catalogue``): :func:`register`
(called explicitly from ``cli/__init__``) installs a factory that builds the
real gated :class:`~teatree.cli.review.service.ReviewService`, and the MCP write
tools reach it only through :mod:`teatree.mcp.review_seam`. The service carries
every publish gate (live-post approval #1207, on-behalf verdict, shape / bloat /
banned-terms scrub), so the MCP surface never bypasses them.
"""

from teatree.cli.review.service import ReviewService
from teatree.mcp.review_seam import register_review_post_seam


def _build_review_service() -> ReviewService:
    return ReviewService(ReviewService.get_gitlab_token())


def register() -> None:
    """Install the gated review-post seam factory into :mod:`teatree.mcp.review_seam`."""
    register_review_post_seam(_build_review_service)
