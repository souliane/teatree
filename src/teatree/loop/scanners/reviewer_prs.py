"""Scan PRs awaiting review from the active user.

Maintains a per-PR ``last_reviewed_sha`` cache so the dispatcher only
fires the reviewer phase agent when the PR has new commits since the
last review pass, OR when the reviewer's prior approval was dismissed
(e.g. invalidated on force-push, re-requested after a dismissal).

The cache lives on ``Ticket(role="reviewer")`` rows in ``extra``
(`reviewed_sha`, `last_review_state`). On first run, any legacy
``loop/reviewer_prs.json`` file is imported into matching tickets and
then deleted — no migration command required.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from teatree.backends.protocols import CodeHostBackend, ReviewState
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models import Ticket as _Ticket

    TicketModel = type[_Ticket]
else:
    TicketModel = object


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cached observation per PR — head sha and last known review state."""

    sha: str = ""
    state: str = ""


def _ticket_model() -> "TicketModel | None":
    """Return the ``core.Ticket`` model, or ``None`` if Django isn't ready."""
    try:
        from django.apps import apps  # noqa: PLC0415

        return cast("TicketModel", apps.get_model("core", "Ticket"))
    except Exception:  # noqa: BLE001
        return None


def _migrate_legacy_json_cache_once() -> None:
    """Import legacy ``loop/reviewer_prs.json`` into reviewer tickets, then delete.

    Idempotent: after the file is removed, subsequent runs are no-ops. Keeps the
    upgrade silent — users never run a migration command.
    """
    import json  # noqa: PLC0415

    from teatree.paths import DATA_DIR  # noqa: PLC0415

    path = DATA_DIR / "loop" / "reviewer_prs.json"
    if not path.is_file():
        return
    ticket_model = _ticket_model()
    if ticket_model is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return
    if not isinstance(data, dict):
        path.unlink(missing_ok=True)
        return
    for url, value in data.items():
        if not isinstance(url, str) or not url:
            continue
        if isinstance(value, str):
            entry = CacheEntry(sha=value, state="")
        elif isinstance(value, dict):
            sha = value.get("sha")
            state = value.get("state")
            entry = CacheEntry(
                sha=sha if isinstance(sha, str) else "",
                state=state if isinstance(state, str) else "",
            )
        else:
            continue
        _persist_entry(ticket_model, url, entry)
    path.unlink(missing_ok=True)


def _persist_entry(ticket_model: "TicketModel", url: str, entry: CacheEntry) -> None:
    """Upsert a reviewer-role ticket's cached SHA/state in its ``extra`` dict."""
    ticket, _ = ticket_model.objects.get_or_create(
        role="reviewer",
        issue_url=url,
        defaults={"overlay": ""},
    )
    extra = dict(ticket.extra or {})
    if entry.sha:
        extra["reviewed_sha"] = entry.sha
    if entry.state:
        extra["last_review_state"] = entry.state
    ticket.extra = extra
    ticket.save(update_fields=["extra"])


def _read_cache() -> dict[str, CacheEntry]:
    """Build the URL → CacheEntry map from reviewer-role tickets."""
    ticket_model = _ticket_model()
    if ticket_model is None:
        return {}
    result: dict[str, CacheEntry] = {}
    rows = ticket_model.objects.filter(role="reviewer").values_list("issue_url", "extra")
    for url, extra in rows:
        if not isinstance(url, str) or not url:
            continue
        if not isinstance(extra, dict):
            continue
        sha = extra.get("reviewed_sha", "")
        state = extra.get("last_review_state", "")
        result[url] = CacheEntry(
            sha=sha if isinstance(sha, str) else "",
            state=state if isinstance(state, str) else "",
        )
    return result


def _head_sha(pr: RawAPIDict) -> str:
    sha = pr.get("sha")
    if isinstance(sha, str):
        return sha
    head = pr.get("head")
    if isinstance(head, dict):
        head_dict = cast("RawAPIDict", head)
        head_sha = head_dict.get("sha")
        if isinstance(head_sha, str):
            return head_sha
    diff_refs = pr.get("diff_refs")
    if isinstance(diff_refs, dict):
        diff_dict = cast("RawAPIDict", diff_refs)
        head_sha = diff_dict.get("head_sha")
        if isinstance(head_sha, str):
            return head_sha
    return ""


def _pr_url(pr: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = pr.get(name)
        if isinstance(value, str):
            return value
    return ""


def _is_dismissed_from_approved(previous: str, current: ReviewState) -> bool:
    """Did the reviewer's prior APPROVED status get invalidated?

    A dismissal is any transition from a recorded ``approved`` state to a
    state where the approval no longer counts: ``DISMISSED`` (explicit) or
    ``PENDING`` (re-requested / dropped on force-push).
    """
    return previous == ReviewState.APPROVED.value and current in {ReviewState.DISMISSED, ReviewState.PENDING}


@dataclass(slots=True)
class ReviewerPrsScanner:
    """Lists PRs where the active user is a requested reviewer.

    Emits ``reviewer_pr.new_sha`` for any PR whose head sha has changed
    since the last cached review pass; ``reviewer_pr.unreviewed`` for
    first-time observations; ``reviewer_pr.approval_dismissed`` when the
    reviewer's prior approval was dropped (forge invalidated it on
    force-push, or the author re-requested review after a dismissal).
    """

    host: CodeHostBackend
    name: str = "reviewer_prs"
    _migrated: bool = field(default=False, init=False)

    def scan(self) -> list[ScanSignal]:
        if not self._migrated:
            _migrate_legacy_json_cache_once()
            self._migrated = True
        reviewer = self.host.current_user()
        if not reviewer:
            return []
        prs = self.host.list_review_requested_prs(reviewer=reviewer)
        cache = _read_cache()
        ticket_model = _ticket_model()
        signals: list[ScanSignal] = []
        for pr in prs:
            url = _pr_url(pr)
            if not url:
                continue
            head = _head_sha(pr)
            previous = cache.get(url, CacheEntry())
            if previous.sha and previous.sha != head:
                signals.append(
                    ScanSignal(
                        kind="reviewer_pr.new_sha",
                        summary=f"Review needed: {url}",
                        payload={"url": url, "head_sha": head, "previous_sha": previous.sha, "raw": pr},
                    ),
                )
                if ticket_model is not None:
                    _persist_entry(ticket_model, url, CacheEntry(sha=head, state=previous.state))
                continue
            if not previous.sha:
                signals.append(
                    ScanSignal(
                        kind="reviewer_pr.unreviewed",
                        summary=f"Review needed: {url}",
                        payload={"url": url, "head_sha": head, "previous_sha": "", "raw": pr},
                    ),
                )
                continue
            current = self.host.get_review_state(pr_url=url, reviewer=reviewer)
            if _is_dismissed_from_approved(previous.state, current):
                signals.append(
                    ScanSignal(
                        kind="reviewer_pr.approval_dismissed",
                        summary=f"Approval dismissed: {url}",
                        payload={
                            "url": url,
                            "head_sha": head,
                            "previous_state": previous.state,
                            "current_state": current.value,
                            "raw": pr,
                        },
                    ),
                )
            if current.value != previous.state and ticket_model is not None:
                _persist_entry(ticket_model, url, CacheEntry(sha=previous.sha, state=current.value))
        return signals


def mark_reviewed(*, url: str, sha: str, state: str = "") -> None:
    """Module-level entry point to update the reviewer cache without owning a scanner instance.

    Called from ``Ticket.mark_reviewed_externally`` when a reviewer-role
    ticket's reviewing task completes — the model layer doesn't need to
    instantiate a backend just to record one observation. ``state`` defaults
    to ``"approved"`` when not supplied so the next scan can detect a
    dismissal of the recorded approval.
    """
    ticket_model = _ticket_model()
    if ticket_model is None:
        return
    resolved_state = state or ReviewState.APPROVED.value
    _persist_entry(ticket_model, url, CacheEntry(sha=sha, state=resolved_state))
