"""Pure URL-prefix specificity scoring for cross-overlay PR attribution (#1324).

A *prefix* claims a set of PR/MR URLs. Two prefix shapes are recognised:

* Plain ``https://host/owner/repo/`` → ``url.startswith(prefix)``.
* Wildcard ``https://host/*/repo/`` → matches any owner segment.

The specificity score (number of fixed, non-wildcard characters that
contributed to the match) breaks ties when two overlays both claim a URL: the
overlay with the exact ``owner/repo`` slug outscores a ``*/repo`` claim and
wins attribution.

Carved below :mod:`teatree.loop.scanners` (and below :mod:`tick_resolvers`,
which re-exports these) as a pure-string leaf so a scanner reaches them
without an up-edge into the orchestration top. Zero teatree imports — a true
``depends_on = []`` leaf.
"""


def url_matches_prefix(url: str, prefix: str) -> bool:
    """Return True when *url* falls inside the *prefix* claim (#1015, #1324).

    Two prefix shapes are recognised:

    * Plain prefix ``https://host/owner/repo/`` → ``url.startswith(prefix)``.
    * Wildcard ``https://host/*/repo/`` → matches any owner segment, so
        ``https://gitlab.com/some-namespace/product/-/merge_requests/1``
        survives a claim of ``https://gitlab.com/*/product/``.

    Centralised here so :class:`MyPrsScanner` and :class:`ReviewerPrsScanner`
    agree on the URL semantics — both honour bare slugs identically without
    each scanner re-implementing the wildcard split.
    """
    return url_match_specificity(url, prefix) > 0


def url_match_specificity(url: str, prefix: str) -> int:
    """Return a specificity score for *prefix* against *url* (#1324).

    ``0`` means no match. A positive score is the number of fixed
    (non-wildcard) characters in *prefix* that contributed to the match —
    used for cross-overlay tie-breaking: ``host/owner/repo/`` (33 chars)
    beats ``host/*/repo/`` (20 chars) when both claim the same URL, so the
    overlay with the exact ``owner/repo`` slug wins attribution.
    """
    if not url or not prefix:
        return 0
    sentinel = "/*/"
    idx = prefix.find(sentinel)
    if idx < 0:
        return len(prefix) if url.startswith(prefix) else 0
    head = prefix[:idx]
    tail = prefix[idx + len(sentinel) :]
    if not url.startswith(head + "/"):
        return 0
    remainder = url[len(head) + 1 :]
    slash = remainder.find("/")
    if slash < 0:
        return 0
    if not remainder[slash + 1 :].startswith(tail):
        return 0
    # Specificity = non-wildcard literals (head + tail), so a strict
    # ``owner/repo`` prefix outscores a ``*/repo`` claim of the same URL.
    return len(head) + len(tail)


def best_url_match_specificity(url: str, prefixes: tuple[str, ...]) -> int:
    """Highest :func:`url_match_specificity` among *prefixes* (#1324).

    Zero when none of the *prefixes* matches *url*. The number is comparable
    across overlays: the scanner uses it to discard a URL claimed less
    specifically here than by a sibling overlay (cross-overlay attribution).
    """
    best = 0
    for prefix in prefixes:
        score = url_match_specificity(url, prefix)
        best = max(best, score)
    return best


__all__ = [
    "best_url_match_specificity",
    "url_match_specificity",
    "url_matches_prefix",
]
