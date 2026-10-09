"""``t3 review`` / ``t3 review-request`` — code-review CLI command package.

Package facade re-exporting the cross-package public surface so the CLI
aggregator and external callers import from ``teatree.cli.review`` while each
symbol keeps an explicit defining submodule (``service`` carries the former
bare ``review.py`` ``ReviewService`` + ``review_app``; ``request`` carries
``review_request_app``). ``mock.patch`` targets name the defining submodule,
never this facade.
"""

from teatree.cli.review.request import review_request_app
from teatree.cli.review.service import ReviewService, review_app

__all__ = ["ReviewService", "review_app", "review_request_app"]
