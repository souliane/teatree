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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from teatree.core.backend_protocols import CodeHostBackend, PrOpenState, ReviewState
from teatree.core.review_candidate import should_review_candidate_reasons
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.url_specificity import best_url_match_specificity
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from teatree.core.models import Ticket as _Ticket
    from teatree.core.models.types import TicketExtra

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
    # #800 N3: canonical locked RMW — this is the THIRD co-writer of
    # reviewed_sha / last_review_state (with _handle_reviewer and
    # Ticket.mark_reviewed_externally); an unlocked save here would still
    # clobber a concurrent pr_urls / visual_qa writer.
    set_keys: TicketExtra = {}
    if entry.sha:
        set_keys["reviewed_sha"] = entry.sha
    if entry.state:
        set_keys["last_review_state"] = entry.state
    if set_keys:
        ticket.merge_extra(set_keys=set_keys)


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


def _pr_url(pr: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = pr.get(name)
        if isinstance(value, str):
            return value
    return ""


def _orphaned_task_signals(
    ticket_model: "TicketModel | None",
    scanned_urls: set[str],
    host: CodeHostBackend,
    overlay: str = "",
) -> list[ScanSignal]:
    """Emit ``reviewer_pr.task_orphaned`` for merged/closed PRs (#1074) or terminal-FSM tickets (#1431).

    Scenario the sweep handles: scanner sees an open MR on tick #1 →
    persistence creates ``Ticket(role=reviewer)`` + ``Task(phase=reviewing,
    status=PENDING)``. The MR is merged/closed externally before the slot
    processes the task. The PENDING task would otherwise linger forever,
    surfacing on every ``pending-spawn`` and dispatching a reviewer
    sub-agent for nothing (#998).

    **The local FSM is authoritative for the user's own decision (#1431).**
    A reviewer ticket whose ``state`` is already terminal
    (DELIVERED/SHIPPED/MERGED/IGNORED) has no legal FSM transition left for
    its reviewing task: a re-dispatched orphan's "nothing to post"
    disposition (``mark_review_no_action``) raises ``TransitionNotAllowed``
    (no terminal state in its ``source=[...]``) and the task re-dispatches
    forever. Such a ticket is reaped on terminal-state *proof* regardless
    of the forge open-state — a self-authored MR the user already concluded
    on legitimately stays OPEN, so a MERGED/CLOSED-only gate never reaches
    it. This is terminal-LOCAL-FSM proof, not absence/UNKNOWN doubt; the
    fail-open default below for non-terminal tickets is untouched.

    **The forge-state decision is state-authoritative, not absence-based (#1074).**
    Absence from ``scanned_urls`` is NOT proof the PR closed:
    ``scanned_urls`` only holds PRs returned by
    ``list_review_requested_prs`` (a reviewer-*assignment* filter). A
    Slack-review-request MR that never got a forge reviewer assignment is
    permanently absent from that scan while still fully OPEN — reaping it
    on absence alone silently drops a live review obligation. So a
    candidate (reviewer-role ticket with a non-terminal reviewing task
    whose URL is absent from the scan) is reaped ONLY when
    ``host.get_pr_open_state`` confirms the PR is genuinely ``MERGED`` or
    ``CLOSED``. ``OPEN`` and ``UNKNOWN`` (auth error, network, unparsable
    URL, draft, anything ambiguous) both skip — fail open, never reap on
    doubt.

    The scanner runs once per (overlay, code-host) pair in ``tick.py``, so
    the orphan sweep must be scoped to the scanner's own overlay — otherwise
    a GitHub scanner pass would sweep GitLab reviewer-role tickets too (and
    vice versa), silently completing legitimate cross-host review tasks
    because their URLs aren't in the GitHub scan's ``scanned_urls``. When
    *overlay* is non-empty we restrict the candidate query to that overlay;
    when empty (the fallback single-overlay path with no tag), the legacy
    unscoped behaviour is preserved.
    """
    if ticket_model is None:
        return []
    # Local import to keep the Django dependency lazy (mirrors _ticket_model).
    from teatree.core.models.task import Task  # noqa: PLC0415

    open_statuses = Task.Status.active()
    candidates = ticket_model.objects.filter(
        role="reviewer",
        tasks__phase="reviewing",
        tasks__status__in=open_statuses,
    )
    if overlay:
        candidates = candidates.filter(overlay=overlay)
    candidates = candidates.exclude(issue_url="").exclude(issue_url__in=scanned_urls).distinct()
    signals: list[ScanSignal] = []
    for ticket in candidates:
        # #1431: the LOCAL FSM is authoritative for the user's own
        # decision. A reviewer ticket whose state is already terminal
        # (DELIVERED/SHIPPED/MERGED/IGNORED) has no legal transition left
        # for its reviewing task — re-dispatch wedges the loop. Reap it
        # regardless of forge state (a self-authored MR with no review owed
        # legitimately stays OPEN). This is terminal-LOCAL-FSM *proof*, not
        # absence/UNKNOWN doubt — the fail-open default below is untouched.
        if ticket.is_terminal:
            signals.append(
                ScanSignal(
                    kind="reviewer_pr.task_orphaned",
                    summary=f"Reviewing task orphaned (ticket terminal: {ticket.state}): {ticket.issue_url}",
                    payload={"url": ticket.issue_url, "ticket_id": ticket.pk},
                ),
            )
            continue
        try:
            state = host.get_pr_open_state(pr_url=ticket.issue_url)
        except Exception:
            logger.exception("ReviewerPrsScanner failed to get PR state for %s", ticket.issue_url)
            continue
        if state not in {PrOpenState.MERGED, PrOpenState.CLOSED}:
            # OPEN → live review still owed; UNKNOWN → fail open. Never reap.
            continue
        signals.append(
            ScanSignal(
                kind="reviewer_pr.task_orphaned",
                summary=f"Reviewing task orphaned (PR {state.value}): {ticket.issue_url}",
                payload={"url": ticket.issue_url, "ticket_id": ticket.pk},
            ),
        )
    return signals


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

    ``identities`` opts the scanner into a multi-alias union query so a
    user with more than one identity on the same forge sees every PR
    where any alias is a requested reviewer. Per-alias dedup-by-url keeps
    a PR that hits two queries from being scanned twice. Empty falls back
    to ``host.current_user()`` (#976).

    ``overlay_name`` scopes the orphan-task sweep to reviewer-role tickets
    belonging to this overlay. Required when running side-by-side scanners
    for multiple overlays/hosts in one tick — a GitHub scanner must not
    sweep GitLab reviewer tickets (#998).

    ``allowed_url_prefixes`` gates emission to PRs whose URL starts with one
    of the listed prefixes — the per-overlay analogue of ``overlay_name``
    for PR-event signals (#1015). Empty tuple preserves legacy behaviour.

    ``competing_url_prefixes`` carries the URL-prefix claims of OTHER
    registered overlays (#1324). When a PR's URL is claimed by both this
    overlay and another, the most-specific claim wins so a dogfooding
    overlay that lists a sibling's repo path does not steal the sibling's
    reviewer-role PRs from its own zone.
    """

    host: CodeHostBackend
    identities: tuple[str, ...] = field(default_factory=tuple)
    overlay_name: str = ""
    allowed_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    competing_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    name: str = "reviewer_prs"
    _migrated: bool = field(default=False, init=False)

    def scan(self) -> list[ScanSignal]:
        if not self._migrated:
            _migrate_legacy_json_cache_once()
            self._migrated = True
        reviewers = self._resolve_identities()
        if not reviewers:
            return []
        primary_reviewer = reviewers[0]
        prs = self._collect_unique_prs(reviewers)
        cache = _read_cache()
        ticket_model = _ticket_model()
        signals: list[ScanSignal] = []
        scanned_urls: set[str] = set()
        for pr in prs:
            url = _pr_url(pr)
            if not url or not self._url_allowed(url):
                continue
            try:
                # #1321 (post-#1328 rewire): the 4 review-candidate skip-conditions
                # belong on the colleague-MR review-sweep path — exactly this loop.
                # ``list_review_requested_prs`` can return MRs the agent must not
                # dispatch ``t3:reviewer`` on (self-authored, self-approved,
                # self-noted, or already merged/closed); filter them here BEFORE
                # adding the URL to ``scanned_urls`` so the orphan-task sweep
                # still reaps the corresponding ticket via ``get_pr_open_state``
                # when the MR is genuinely merged/closed. The full identity set
                # (not just the primary alias) is matched so an MR authored under
                # any of the user's github/gitlab aliases is recognised as own
                # work (#1321 multi-identity).
                reasons = should_review_candidate_reasons(pr, current_user=primary_reviewer, self_identities=reviewers)
                if reasons:
                    # #1321: a reviewing task already created for a self-authored
                    # OPEN MR (before this gate, or via another path) lingers
                    # forever — the orphan sweep only reaps MERGED/CLOSED PRs.
                    # Emit a reconciliation signal so the queue self-heals on the
                    # next tick. Other skip reasons (already-approved, merged,
                    # broadcast-reacted) are handled by the existing orphan sweep
                    # or are genuinely review-engaged, so only ``author_is_self``
                    # drives reconciliation here.
                    if "author_is_self" in reasons:
                        signals.extend(self._self_authored_reconcile_signals(url, ticket_model))
                    continue
                scanned_urls.add(url)
                signals.extend(self._signals_for_pr(pr, url, cache, ticket_model, primary_reviewer))
            except Exception:
                logger.exception("ReviewerPrsScanner failed on PR %s", url)
                continue
        signals.extend(_orphaned_task_signals(ticket_model, scanned_urls, self.host, self.overlay_name))
        return signals

    def _self_authored_reconcile_signals(
        self,
        url: str,
        ticket_model: "TicketModel | None",
    ) -> list[ScanSignal]:
        """Emit ``reviewer_pr.task_self_authored`` for an open reviewing task on a self-authored MR (#1321).

        A reviewer-role ticket carrying a non-terminal ``reviewing`` task
        whose MR the user authored is wrong — own MRs route to coder/
        debugger + a colleague review-request, never a ``t3:reviewer``
        sub-agent. The mechanical handler completes the task so
        ``pending-spawn`` stops surfacing it.
        """
        if ticket_model is None or not url:
            return []
        from teatree.core.models.task import Task  # noqa: PLC0415

        open_statuses = Task.Status.active()
        candidates = ticket_model.objects.filter(
            role="reviewer",
            issue_url=url,
            tasks__phase="reviewing",
            tasks__status__in=open_statuses,
        )
        if self.overlay_name:
            candidates = candidates.filter(overlay=self.overlay_name)
        return [
            ScanSignal(
                kind="reviewer_pr.task_self_authored",
                summary=f"Reviewing task closed (self-authored MR): {url}",
                payload={"url": url, "ticket_id": ticket.pk},
            )
            for ticket in candidates.distinct()
        ]

    def _signals_for_pr(
        self,
        pr: RawAPIDict,
        url: str,
        cache: dict[str, CacheEntry],
        ticket_model: "TicketModel | None",
        primary_reviewer: str,
    ) -> list[ScanSignal]:
        """Emit the review signals (new_sha / unreviewed / approval_dismissed) for one PR."""
        head = head_sha(pr)
        previous = cache.get(url, CacheEntry())
        if previous.sha and previous.sha != head:
            if ticket_model is not None:
                _persist_entry(ticket_model, url, CacheEntry(sha=head, state=previous.state))
            return [
                ScanSignal(
                    kind="reviewer_pr.new_sha",
                    summary=f"Review needed: {url}",
                    payload={"url": url, "head_sha": head, "previous_sha": previous.sha, "raw": pr},
                ),
            ]
        if not previous.sha:
            return [
                ScanSignal(
                    kind="reviewer_pr.unreviewed",
                    summary=f"Review needed: {url}",
                    payload={"url": url, "head_sha": head, "previous_sha": "", "raw": pr},
                ),
            ]
        # Query review state under one canonical alias — the cache tracks one
        # entry per URL, so a per-alias query would just race the persist
        # back to itself.
        current = self.host.get_review_state(pr_url=url, reviewer=primary_reviewer)
        signals: list[ScanSignal] = []
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

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _url_allowed(self, url: str) -> bool:
        """Same per-overlay URL-prefix gate as ``MyPrsScanner._url_allowed`` (#1015, #1324)."""
        if not self.allowed_url_prefixes:
            return True
        if not url:
            return False
        own = best_url_match_specificity(url, self.allowed_url_prefixes)
        if own == 0:
            return False
        competing = best_url_match_specificity(url, self.competing_url_prefixes)
        return competing <= own

    def _collect_unique_prs(self, reviewers: tuple[str, ...]) -> list[RawAPIDict]:
        """Union review-requested PRs across *reviewers*, deduped by URL."""
        seen_urls: set[str] = set()
        prs: list[RawAPIDict] = []
        for reviewer in reviewers:
            for pr in self.host.list_review_requested_prs(reviewer=reviewer):
                url = _pr_url(pr)
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                prs.append(pr)
        return prs


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
