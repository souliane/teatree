"""Typed exceptions shared across all backend implementations.

These exceptions give the scanner layer a clean, backend-agnostic way to
distinguish definitive failures (404 — remote resource gone for good) from
transient ones (5xx, timeout, network error — worth retrying next tick).

Every ``CodeHostBackend.get_issue`` implementation MUST raise
``IssueNotFoundError`` when and ONLY when the remote forge returns HTTP 404.
All other failures (5xx, timeout, connection error) must propagate as-is so
the scanner's generic ``except Exception`` path keeps retrying them.
"""

__all__ = ["IssueNotFoundError"]


class IssueNotFoundError(Exception):
    """Raised by a ``CodeHostBackend`` when the remote issue returns HTTP 404.

    The scanner catches this specific exception to mark the local
    :class:`~teatree.core.models.ticket.Ticket` as ``remote_missing`` and stop
    re-fetching it on every tick. Any other exception class signals a transient
    failure (5xx, timeout, connection reset) and the ticket keeps retrying.
    """

    def __init__(self, issue_url: str) -> None:
        self.issue_url = issue_url
        super().__init__(f"Remote issue not found (HTTP 404): {issue_url}")
