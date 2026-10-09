"""Compare and remove one marked issue-description section."""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.self_forge_identities import require_self_authored_issue
from teatree.types import RawAPIDict


class DescriptionRemovalConflictError(Exception):
    """The requested section could not be safely removed or confirmed absent."""


@dataclass(frozen=True, slots=True)
class DescriptionRemoval:
    marker: str
    expected_body: str
    replacement_body: str
    action: str
    retain_marker: bool = False
    generated_span: tuple[int, int] | None = None


def _description(issue: RawAPIDict) -> str:
    for key in ("description", "body"):
        value = issue.get(key)
        if isinstance(value, str):
            return value
    return ""


def remove_description_section(
    *,
    host: CodeHostBackend,
    issue_url: str,
    removal: DescriptionRemoval,
    scrub: Callable[[str], str],
    write: Callable[[str], object],
) -> None:
    """Edit one marker after author and current-body checks, scrubbing only generated text."""
    current = _description(require_self_authored_issue(host=host, issue_url=issue_url))
    if removal.marker not in current:
        return
    if current != removal.expected_body or (not removal.retain_marker and removal.marker in removal.replacement_body):
        msg = f"description changed before removal of {removal.marker} on {issue_url}"
        raise DescriptionRemovalConflictError(msg)
    clean = removal.replacement_body
    if removal.generated_span is not None:
        start, end = removal.generated_span
        clean = clean[:start] + scrub(clean[start:end]) + clean[end:]
    if _description(host.get_issue(issue_url)) != current:
        msg = f"description changed before removal of {removal.marker} on {issue_url}"
        raise DescriptionRemovalConflictError(msg)
    write(clean)
    if _description(host.get_issue(issue_url)) != clean:
        if removal.retain_marker:
            msg = f"could not confirm description edit of {removal.marker} on {issue_url}"
        else:
            msg = f"could not confirm complete removal of {removal.marker} from {issue_url}"
        raise DescriptionRemovalConflictError(msg)
