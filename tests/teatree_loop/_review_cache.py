"""Set up observed forge review state for scanner tests."""

from teatree.loop.scanners.reviewer_prs import CacheEntry, _persist_entry, _ticket_model


def seed_review_state(*, url: str, sha: str, state: str) -> None:
    ticket_model = _ticket_model()
    if ticket_model is not None:
        _persist_entry(ticket_model, url, CacheEntry(sha=sha, state=state))
