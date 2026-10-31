"""Stand in for the forge's issue read that the acceptance-item checks make (``core.gates.issue_coverage``)."""

import contextlib
from collections.abc import Generator
from unittest.mock import MagicMock, patch


class _Provider:
    def __init__(self, host: MagicMock) -> None:
        self.host = host

    def get_code_host_for_url(self, overlay: object, issue_url: str) -> MagicMock:
        _ = (overlay, issue_url)
        return self.host


@contextlib.contextmanager
def issue_read(payload: dict[str, str] | Exception) -> Generator[MagicMock]:
    """Answer ``get_issue`` with *payload* (or raise it); yields the host so a test can assert it was not asked."""
    host = MagicMock()
    if isinstance(payload, Exception):
        host.get_issue.side_effect = payload
    else:
        host.get_issue.return_value = payload
    with (
        patch("teatree.core.gates.issue_coverage.get_backend_provider", return_value=_Provider(host)),
        patch("teatree.core.gates.issue_coverage.get_overlay_for_ticket", return_value=object()),
    ):
        yield host
