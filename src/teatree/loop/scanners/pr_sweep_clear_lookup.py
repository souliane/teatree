"""Locate the ``MergeClear`` authorising one ``(repo, pr, head)`` — and the near-miss.

``MergeClear.slug`` holds a workstream name, a repo, or a head BRANCH: nothing rejects
a branch, because ``_looks_like_owner_repo`` judges from string shape. The merge path
reconciles that; an exact ``slug=`` join cannot, so the row stayed actionable and
invisible for as long as it existed (#4249). This module is the sweep's single answer to
"which CLEAR is for this PR?", resolving the slug the same way the merge does.
"""

from dataclasses import dataclass

from teatree.core.merge import fallback_repo_slug, normalize_repo_slug, slug_is_registered_repo
from teatree.core.models.merge_clear import MergeClear


@dataclass(frozen=True, slots=True)
class ClearLookup:
    """What the sweep found for one ``(repo, pr, head)`` — the match AND the near-miss.

    ``unusable`` is a CLEAR scoped to this very PR that cannot authorise the LIVE
    head (issued against a since-superseded tree, or missing a load-bearing field).
    Carrying it out is what lets the scanner say "a CLEAR exists that I could not
    match" instead of reporting that absent signal as a verdict about review.
    """

    clear: MergeClear | None
    unusable: MergeClear | None


def clear_scopes_to_repo(clear: MergeClear, *, slug: str) -> bool:
    """True iff *clear* authorises a PR in the repo *slug* names.

    Only a slug the registry NAMES as a repo is a claim strong enough to exclude
    another repo's sweep; every other shape resolves through the ticket / clone
    fallback the merge path already walks, so the lookup and the merge stop holding
    two different slug semantics.
    """
    canonical = normalize_repo_slug(slug)
    if not canonical:
        return False
    row = normalize_repo_slug(clear.slug)
    if row == canonical:
        return True
    if row and slug_is_registered_repo(row):
        return False
    return normalize_repo_slug(fallback_repo_slug(clear)) == canonical


def look_up_clear_for_head(*, slug: str, pr_id: int, head_sha: str) -> ClearLookup:
    """The actionable SHA-matched CLEAR for *(slug, pr_id, head_sha)*, plus the near-miss.

    A row whose ``reviewed_sha`` does not match the live PR head cannot authorise the
    merge (§17.4.2 binds the authorisation to the exact reviewed tree) — but it is no
    longer discarded silently: it comes back as :attr:`ClearLookup.unusable` so the
    caller reports a present-but-unusable CLEAR distinctly from none at all. The
    keystone transition re-validates SHA-match at merge time as well, so even a stale
    match here would be refused.
    """
    matched: MergeClear | None = None
    unusable: MergeClear | None = None
    candidates = (
        MergeClear.objects.filter(pr_id=pr_id, consumed_at__isnull=True).select_related("ticket").order_by("-issued_at")
    )
    for clear in candidates:
        if not clear_scopes_to_repo(clear, slug=slug):
            continue
        if clear.reviewed_sha == head_sha and clear.is_actionable():
            matched = matched or clear
        elif unusable is None:
            unusable = clear
    return ClearLookup(clear=matched, unusable=unusable)
