"""Ownership guard — exclude colleague-authored items on product repos up front (#2763).

The COLLABORATION axis applied to cleanup (see ``/t3:rules`` § "Three Orthogonal
Repo Axes"). The owner's own solo repos are swept freely — every branch/worktree
there is the owner's. The org's shared PRODUCT repos are different: a branch or
worktree whose author is NOT the owner is a colleague's work and must be EXCLUDED
from every pass — never deleted, never emitted for deletion.

Data-driven, never hardcoded. A repo is "colleague-facing" iff its remote slug
matches the configured ``colleague_repo_url_pattern`` regex (empty by default, so
the guard is a no-op for the owner's solo repos — it stays in code for when a
product overlay sets the pattern). Ownership is resolved against the owner's
identity set: the configured ``user_identity_aliases`` plus the repo's local
``git config user.name`` / ``user.email`` — reusing
:func:`teatree.core.review.review_candidate.author_is_self`. An item whose author is not
in that set, on a colleague-facing repo, is excluded.
"""

import logging
import re
from dataclasses import dataclass

from teatree.core.review.review_candidate import author_is_self
from teatree.utils import git
from teatree.utils.git_remote_ops import config_value, remote_slug
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OwnershipVerdict:
    """Whether an item is excluded up front because a colleague authored it.

    ``excluded`` is ``True`` only on a colleague-facing PRODUCT repo whose item
    author is not one of the owner's identities. ``reason`` is the phrase the
    reaper logs so an exclusion is never silent.
    """

    excluded: bool
    reason: str = ""


def _is_colleague_repo(repo: str, colleague_pattern: str) -> bool:
    """True iff the repo's origin slug/url matches the colleague-facing pattern."""
    if not colleague_pattern:
        return False
    slug = remote_slug(repo) or git.remote_url(repo)
    if not slug:
        return False
    try:
        return re.search(colleague_pattern, slug) is not None
    except re.error:
        logger.warning("cleanup_ownership: invalid colleague_repo_url_pattern %r — treating as solo", colleague_pattern)
        return False


def _branch_author(repo: str, ref: str) -> tuple[str, str]:
    """Return ``(author_name, author_email)`` of ``ref``'s tip commit, or ``("", "")``."""
    raw = git.run(repo=repo, args=["log", "-1", "--format=%an%x00%ae", ref])
    name, _, email = raw.partition("\x00")
    return name.strip(), email.strip()


def _owner_identities(repo: str, configured_aliases: list[str]) -> set[str]:
    """The owner's full identity set: configured aliases plus the repo's git identity."""
    identities = {alias for alias in configured_aliases if alias}
    for key in ("user.name", "user.email"):
        try:
            value = config_value(repo, key).strip()
        except CommandFailedError:
            value = ""
        if value:
            identities.add(value)
    return identities


def is_excluded_by_ownership(
    repo: str, ref: str, *, owner_aliases: list[str], colleague_pattern: str
) -> OwnershipVerdict:
    """Whether ``ref`` on ``repo`` is a colleague's work on a product repo — exclude up front.

    Returns not-excluded for the owner's solo repos (no colleague pattern, or the
    slug doesn't match it) — the common single-author case where the guard is a
    no-op. On a colleague-facing repo it resolves the tip author and reuses
    :func:`author_is_self`: an author outside the owner's identity set is excluded;
    an unresolvable author fails SAFE to EXCLUDED (never delete a colleague's work
    on uncertainty).
    """
    if not _is_colleague_repo(repo, colleague_pattern):
        return OwnershipVerdict(excluded=False)
    name, email = _branch_author(repo, ref)
    if not name and not email:
        return OwnershipVerdict(excluded=True, reason="could not resolve the author on a product repo — excluding")
    identities = _owner_identities(repo, owner_aliases)
    primary = next(iter(identities), "")
    if author_is_self(name, current_user=primary, self_identities=identities) or author_is_self(
        email, current_user=primary, self_identities=identities
    ):
        return OwnershipVerdict(excluded=False)
    return OwnershipVerdict(excluded=True, reason=f"colleague-authored ({name or email}) on a product repo — excluding")
