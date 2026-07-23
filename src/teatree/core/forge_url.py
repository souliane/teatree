"""Classify a ``Ticket.issue_url``: real forge URL, local anchor, or synthetic sentinel.

Four shapes reach this field, and ``derive_issue_number`` renders all of them as
a forge-looking number, so they are indistinguishable downstream once rendered:

- a real ``http(s)://…`` issue/PR URL — deliverable work on a forge;
- a local anchor (``""``, ``auto:<branch>``) — deliverable work with no forge
    issue behind it;
- a synthetic loop-cadence anchor (``architectural-review://<overlay>``,
    ``eval-local://…``, ``scanning-news://…``, ``dogfood-smoke://…``) — a
    recurring schedule that permanently sits at ``not_started`` and has no
    terminal state to reach;
- malformed debris (a bare ``"3274"``) written before ``canonicalize_issue_ref``
    guarded the intake seam; the correctly-formed row for the same issue exists
    separately.

The last two are not deliverable work. Conflating them with the first two is how
a doctor probe came to report cadence anchors and bare-number debris as frozen
work, pinning a permanent unactionable FAIL (souliane/teatree#3492).
"""

_HTTP_SCHEMES = ("http://", "https://")
_SCHEME_SEPARATOR = "://"


def is_forge_url(issue_url: str) -> bool:
    """Whether *issue_url* is a real ``http(s)`` forge URL."""
    return issue_url.startswith(_HTTP_SCHEMES)


def is_synthetic_ticket_url(issue_url: str) -> bool:
    """Whether *issue_url* names no deliverable work at all.

    True for a loop-cadence anchor (a non-``http(s)`` ``<scheme>://<overlay>``
    key) and for bare-number debris. False for a real forge URL AND for a local
    anchor (``""`` / ``auto:<branch>``), which is ordinary work carrying no forge
    issue — excluding those would trade a false positive for a false negative.
    """
    if is_forge_url(issue_url):
        return False
    return _SCHEME_SEPARATOR in issue_url or issue_url.isdigit()
