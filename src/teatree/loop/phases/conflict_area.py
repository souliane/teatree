"""A CHEAP area key + spread for prioritising non-conflicting work (#3634).

Deliberately NOT conflict prediction: no diff fetch, no merge-base probe, no
file-level analysis. The area of a ticket is the repo (or repo set) it declares,
which is already on the row — so ordering costs one dict lookup per candidate.

:func:`spread_by_area` re-orders candidates round-robin across areas so a WRITE
fan-out picks work in different repos before it picks a second ticket in the same
one. It only re-orders — nothing is ever dropped, so an unknowable area (an empty
key) is one bucket like any other and can never starve.
"""

from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urlparse

from teatree.utils.url_slug import slug_from_issue_or_pr_url


def area_key(*, repos: Iterable[str], issue_url: str) -> str:
    """The cheap area key: the declared repo set, else the issue URL's repo slug."""
    declared = sorted({repo.strip() for repo in repos if repo.strip()})
    if declared:
        return "+".join(declared)
    return slug_from_issue_or_pr_url(urlparse(issue_url).path)


def spread_by_area[T](items: Sequence[T], *, key: Callable[[T], str]) -> list[T]:
    """Round-robin *items* across their areas, stable within each area."""
    buckets: dict[str, list[T]] = {}
    for item in items:
        buckets.setdefault(key(item), []).append(item)
    spread: list[T] = []
    while buckets:
        for area in list(buckets):
            spread.append(buckets[area].pop(0))
            if not buckets[area]:
                del buckets[area]
    return spread
