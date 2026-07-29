"""Derive a clickable link + short ref for a ticket's ``issue_url``.

The dashboard card and drawer show a ticket's forge issue as a clickable ref
(``#3205`` for an issue, ``!N`` for a pull/merge request). Synthetic loop keys
(``scanning-news://…``, ``eval-local://…``, ``dogfood-smoke://…``) are NOT
forge URLs and must never render as a link — they resolve to ``("", "")`` so
the template renders plain text instead of a dead anchor.
"""

from teatree.core.forge_url import is_forge_url
from teatree.core.models.ticket_number import derive_issue_number

_REQUEST_MARKERS = ("/pull/", "/merge_requests/")


def issue_link(issue_url: str) -> tuple[str, str]:
    """``(href, ref)`` for *issue_url*; ``("", "")`` for non-forge sentinels.

    *href* is the URL itself, only when it is an ``http(s)`` forge URL. *ref* is
    a short human handle: ``!N`` for a pull/merge request, ``#N`` for an issue,
    or the trailing path segment when no numeric id is present.
    """
    if not is_forge_url(issue_url):
        return "", ""
    number = derive_issue_number(issue_url)
    if any(marker in issue_url for marker in _REQUEST_MARKERS):
        return issue_url, f"!{number}" if number else "!"
    if number:
        return issue_url, f"#{number}"
    return issue_url, issue_url.rstrip("/").rsplit("/", 1)[-1]
