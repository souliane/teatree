"""A code host that answers one pull-request author, for tests that cross a review-authorship check."""

from dataclasses import dataclass
from unittest.mock import patch

_REACTIONS = "teatree.loop.review_done_reactions"


@dataclass(frozen=True)
class AuthorForge:
    author: str = ""
    error: Exception | None = None

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        if self.error is not None:
            raise self.error
        return self.author


def reaction_forge(forge: AuthorForge | None):
    """Replace the code host the review-DONE reaction reads the author from."""
    return patch(f"{_REACTIONS}.code_host_from_overlay", return_value=forge)


def colleague_forge():
    return reaction_forge(AuthorForge(author="mallory"))
