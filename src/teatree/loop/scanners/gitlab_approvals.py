"""Poll-driven GitLab MR approval scanner — #936 phase 1.

The webhook path (``IncomingEventsScanner`` + ``SCHEDULE_MERGE``) already
drives the sanctioned auto-merge keystone when GitLab fires an
``approved`` webhook to ``/hooks/gitlab/``. That path is blocked for
Slack Connect workspaces where the bot cannot join the overlay's
review channel, and other deployments that have not enabled the GitLab
webhook at all. This scanner is the
poll-driven complement: every tick it walks the active user's open MRs,
asks GitLab for the approval state, and — when the merge guard says yes
— emits the same ``incoming_event.merge_*`` signal the dispatcher
already routes to the §17.4 keystone merge transition.

Design notes
------------

* No new merge code. The scanner emits ``ScanSignal``s only; the
    existing dispatcher + ``OverlayBase.can_auto_merge`` guard +
    ``t3 <overlay> ticket merge`` keystone handle the actual write.
* Idempotency. The head SHA at the moment of emission is recorded in
    ``Ticket.extra['last_approval_sha']``; a second tick whose payload
    carries the same head SHA is a no-op. A new push (different head SHA)
    resets the window — approval must be re-acquired on the new commit.
* GitHub silently skipped. The URL filter (:func:`is_gitlab_mr_url`) drops a
    GitHub PR before any approval call, so a mixed-host overlay never pays the
    round-trip — the merge signal this scanner drives is GitLab-only.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import httpx

import teatree.core.overlay_loader as _overlay_loader
from teatree.core.backend_protocols import ApprovalState, CodeHostBackend
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal, SignalPayload
from teatree.types import RawAPIDict
from teatree.url_classify import is_gitlab_mr_url, pr_ref

if TYPE_CHECKING:
    from teatree.core.gates.merge_guard import MergeGuard
    from teatree.core.models import Ticket as _Ticket

    TicketModel = type[_Ticket]
else:
    TicketModel = object

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GitLabApprovalsScanner:
    """Poll active user's open GitLab MRs and emit merge signals on approval.

    ``identities`` mirrors :class:`MyPrsScanner` — the multi-alias union
    query used when the user has more than one identity on the same forge.
    Empty falls back to ``host.current_user()``.
    """

    host: CodeHostBackend
    identities: tuple[str, ...] = field(default_factory=tuple)
    name: str = "gitlab_approvals"

    def scan(self) -> list[ScanSignal]:
        authors = self._resolve_identities()
        if not authors:
            return []
        signals: list[ScanSignal] = []
        for pr in self._collect_unique_prs(authors):
            try:
                signal = self._scan_one(pr)
            except ScannerError:
                raise  # auth/network escalation — must surface to the dispatcher
            except Exception:
                logger.exception(
                    "GitLabApprovalsScanner: _scan_one failed for %s",
                    _str_field(pr, "web_url", "html_url"),
                )
                continue
            if signal is not None:
                signals.append(signal)
        return signals

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _collect_unique_prs(self, authors: tuple[str, ...]) -> list[RawAPIDict]:
        seen_urls: set[str] = set()
        prs: list[RawAPIDict] = []
        for author in authors:
            for pr in self.host.list_my_prs(author=author):
                url = _str_field(pr, "web_url", "html_url")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                prs.append(pr)
        return prs

    def _scan_one(self, pr: RawAPIDict) -> ScanSignal | None:
        # GitLab MRs carry a numeric ``iid`` and a ``project`` reference;
        # GitHub PRs carry a ``number`` and an ``html_url`` shape. The
        # scanner only drives the GitLab auto-merge signal, so a GitHub PR is
        # dropped here by the URL host filter — never reaching the approval
        # call, which is why differentiating on the URL avoids the round-trip.
        url = _str_field(pr, "web_url", "html_url")
        ref = pr_ref(url) if url else None
        repo_slug = ref.slug if ref is not None and ref.host_kind == "gitlab" else ""
        iid = _int_field(pr, "iid", "number")
        head_sha = _str_field(pr, "sha", "head_sha")
        if not url or not is_gitlab_mr_url(url) or not repo_slug or not iid:
            return None

        approvals = self._fetch_approvals(repo_slug, iid)
        if approvals is None or approvals["approvals_left"] > 0:
            # ``None`` → a transient error swallowed per-MR. ``approvals_left
            # > 0`` → not approved yet; the not-yet-approved case is steady
            # state, not "blocked".
            return None

        # Approved. Idempotency gate: same head SHA as the last emission?
        if _already_emitted_at(url, head_sha):
            return None

        target_ref = _str_field(pr, "target_branch")
        title = _str_field(pr, "title")
        guard = _overlay_loader.get_overlay_for_url(url).review.can_auto_merge(
            target_ref=target_ref or url,
            thread_ref=url,
        )
        signal = _signal_for(
            guard=guard,
            url=url,
            target_ref=target_ref,
            unresolved=approvals["unresolved_resolvable"],
            title=title,
        )
        if signal is not None:
            _record_emission(url, head_sha)
        return signal

    def _fetch_approvals(self, repo_slug: str, iid: int) -> ApprovalState | None:
        """Fetch approvals; return ``None`` on a transient defect, raise on auth failure.

        Only reached for a GitLab MR URL (the ``_scan_one`` host filter drops a
        GitHub PR first), so ``get_mr_approvals`` is always the live GitLab
        implementation here. An ``httpx.HTTPStatusError`` or any other
        ``httpx.HTTPError`` is translated into ``ScannerError`` so the dispatcher
        records the error and DMs the user (#1287) — the previous ``return None``
        silently converted a 401 into "not approved yet". Anything else is
        logged and returned as ``None`` so a transient defect on one MR does not
        break the scan for the others.
        """
        try:
            return self.host.get_mr_approvals(repo=repo_slug, pr_iid=iid)
        except ScannerError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ScannerError(
                scanner="gitlab_approvals",
                error_class=_classify_http_status(exc.response.status_code),
                detail=f"GitLab {repo_slug} !{iid}: HTTP {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise ScannerError(
                scanner="gitlab_approvals",
                error_class=ScannerErrorClass.NETWORK,
                detail=f"GitLab {repo_slug} !{iid}: {type(exc).__name__}",
            ) from exc
        except Exception:
            logger.exception("GitLabApprovalsScanner: get_mr_approvals failed for %s !%d", repo_slug, iid)
            return None


def _signal_for(
    *,
    guard: "MergeGuard",
    url: str,
    target_ref: str,
    unresolved: int,
    title: str,
) -> ScanSignal | None:
    """Translate the merge-guard verdict + payload into a ``ScanSignal``.

    Splits ``allowed=False`` into two outputs:

    * ``escalate=True``  → ``incoming_event.merge_escalation``
    * ``escalate=False`` → ``incoming_event.merge_blocked`` (an approved
        MR that still has unresolved resolvable threads, OR an overlay
        policy block)
    """
    base_payload: SignalPayload = {
        "event_id": None,
        "target_ref": target_ref,
        "thread_ref": url,
    }
    # An unresolved-resolvable thread on an otherwise-approved MR blocks the
    # merge under upstream's "must resolve all threads" policy. We surface this
    # as merge_blocked even when the overlay guard is permissive — the upstream
    # gate is real, the keystone would reject the merge, so a merge_needed
    # would be a false positive. The overlay guard still gets a chance to
    # escalate.
    if guard.allowed and unresolved > 0:
        return ScanSignal(
            kind="incoming_event.merge_blocked",
            summary=f"merge blocked on {target_ref or url}: {unresolved} unresolved thread(s) — {title}",
            payload={**base_payload, "reason": f"unresolved resolvable threads: {unresolved}"},
        )
    if guard.allowed:
        return ScanSignal(
            kind="incoming_event.merge_needed",
            summary=f"merge approved on {target_ref or url} ({url}): {title}",
            payload={**base_payload, "reason": "approved"},
        )
    if guard.escalate:
        return ScanSignal(
            kind="incoming_event.merge_escalation",
            summary=f"merge escalation on {target_ref or url}: {guard.reason}",
            payload={**base_payload, "reason": guard.reason},
        )
    return ScanSignal(
        kind="incoming_event.merge_blocked",
        summary=f"merge blocked on {target_ref or url}: {guard.reason}",
        payload={**base_payload, "reason": guard.reason},
    )


_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY_REQUESTS = 429


def _classify_http_status(status_code: int) -> ScannerErrorClass:
    """Classify an upstream HTTP error code into a :class:`ScannerErrorClass` (#1287).

    GitLab returns 401 for missing / expired tokens, 403 for
    insufficient scope, and 429 for rate limit. Everything else falls
    through to :attr:`ScannerErrorClass.UNKNOWN` so the dispatcher still
    surfaces the failure to the user.
    """
    if status_code == _HTTP_UNAUTHORIZED:
        return ScannerErrorClass.AUTH
    if status_code == _HTTP_FORBIDDEN:
        return ScannerErrorClass.MISSING_SCOPE
    if status_code == _HTTP_TOO_MANY_REQUESTS:
        return ScannerErrorClass.RATE_LIMIT
    return ScannerErrorClass.UNKNOWN


def _str_field(data: RawAPIDict, *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _int_field(data: RawAPIDict, *names: str) -> int:
    for name in names:
        value = data.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _ticket_model() -> "TicketModel | None":
    try:
        from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

        return cast("TicketModel", apps.get_model("core", "Ticket"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


def _already_emitted_at(url: str, head_sha: str) -> bool:
    """Return True iff this scanner emitted at the same head SHA on a prior tick.

    Treats a missing SHA as "no idempotency available" — a single empty-SHA
    payload de-dupes against any prior empty-SHA emission, but a re-emission
    with a SHA always wins (so the first useful emission lands). When Django
    is not ready (boot path, test harness without a ``db`` marker), returns
    False so the scanner stays useful in those modes.
    """
    if not url:
        return False
    ticket_model = _ticket_model()
    if ticket_model is None:
        return False
    try:
        ticket = ticket_model.objects.filter(issue_url=url).first()
    except Exception:  # noqa: BLE001 — a lookup failure degrades to not-approved, never breaks the scan
        return False
    if ticket is None:
        return False
    # Reach via the validator (mirrors ``Ticket._extra`` private accessor),
    # avoiding the SLF001 violation while keeping the JSON-key contract.
    from teatree.core.models.types import validated_ticket_extra  # noqa: PLC0415 — deferred: ORM/app-registry

    return validated_ticket_extra(ticket.extra).get("last_approval_sha", "") == head_sha


def _record_emission(url: str, head_sha: str) -> None:
    """Persist the head SHA of the latest emission on an existing ``Ticket`` row.

    Only updates a row that already exists — never creates a phantom
    blank-overlay Ticket for a URL that has no real ticket. Best effort:
    a DB error or a missing ticket are both non-fatal (the cost of failing
    to dedup is one extra signal, not a wrong merge).
    """
    if not url:
        return
    ticket_model = _ticket_model()
    if ticket_model is None:
        return
    try:
        ticket = ticket_model.objects.filter(issue_url=url).first()
        if ticket is None:
            return
        ticket.merge_extra(set_keys={"last_approval_sha": head_sha})
    except Exception:
        logger.exception("GitLabApprovalsScanner: could not persist last_approval_sha for %s", url)


__all__ = ["ApprovalState", "GitLabApprovalsScanner"]
