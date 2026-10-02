"""Whether the OWNER authored the MR a review post targets (#960 author-side carve-out).

The on-behalf pre-gate exempts an author-side reply on the owner's OWN MR
(``teatree.on_behalf_gate._AUTHOR_SIDE_ACTIONS``), but the pure resolver
depends only on :mod:`teatree.config` and cannot read a forge. This module
is the proof the resolver requires.

The author read is :func:`~teatree.cli.review.shape_gate.fetch_mr_author`,
already the one MR-author read in this package (response-cached per
``(repo, mr)``, so the gate's peek and its publish cost one GET between
them). What is NOT reused is its sibling ``is_colleague_mr``: that gate
fails OPEN — an unreadable author reads as "own MR", which merely relaxes a
prose cap. Here the same neutral would relax a CONSENT gate, so the polarity
is inverted and stated once: this module reports ``True`` only when the
author is PROVED to be the owner. An unreadable MR, an author-less payload,
an unresolvable identity, or any transport failure all report ``False`` and
leave the post gated exactly as before.

Identity is the union of the POSTING credential's own ``current_username``
(the strongest signal — it IS the identity the reply publishes as) and the
configured ``user_identity_aliases``, so an MR authored under a secondary
forge alias is still recognised as the owner's own work.
"""

from typing import TYPE_CHECKING

from teatree.cli.review.guarded_read import guarded_read
from teatree.cli.review.shape_gate import fetch_mr_author
from teatree.core.review.review_candidate import author_is_self

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabAPI


def owner_authored_mr(api: "GitLabAPI", repo: str, mr: int) -> bool:
    """Whether *repo*!*mr* was authored by one of the owner's own forge identities.

    The single seam the review CLI uses to prove the author-side carve-out's
    precondition. Never raises; every failure reports ``False``.
    """
    author = fetch_mr_author(api, repo.replace("/", "%2F"), mr)
    if not author:
        return False
    identity = guarded_read("the posting GitLab identity", api.current_username, neutral="")
    aliases = guarded_read("the owner's configured forge handles", _configured_aliases, neutral=())
    return author_is_self(author, current_user=identity.value, self_identities=aliases.value)


def _configured_aliases() -> tuple[str, ...]:
    """The owner's configured forge handles (``user_identity_aliases``)."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light

    return tuple(get_effective_settings().user_identity_aliases)


__all__ = ["owner_authored_mr"]
