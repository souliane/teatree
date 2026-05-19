"""Outbound-audit drift verifier (#1019).

On every tick this scanner picks up :class:`OutboundClaim` rows whose
``claim_ts`` is older than a per-kind *settling window* and that have
neither been verified nor flagged. For each, it asks the third-party
system whether the artifact actually exists. If it does, the row is
marked ``verified_at = now()``; if not, the row is flipped to
``drift_detected = True``, the user is DM'd via the injected
``notifier`` callable (the CLI wires ``notify_user`` in), and
``drift_alerted_at`` is set so the same drift does not re-alert on the
next tick.

Settling windows are per-kind and configurable via the module-level
``kind_settling_seconds`` dict (Notion is slower than Slack). The
default windows are conservative; lower them if drift detection latency
matters more than false-positive risk.

Verifier functions are injected at scanner construction so tests can mock
the third-party calls. In production, the scanner falls back to building
its own ``GitLabAPI`` / ``SlackBot`` / ``NotionClient`` from the overlay
factory; missing credentials degrade gracefully (the row is skipped
without alerting, so a temporarily-unreachable API doesn't spam DMs).
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, ClassVar, cast

import httpx
from django.apps import apps
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import OutboundClaim as OutboundClaimModel
    from teatree.types import RawAPIDict


type DriftNotifier = Callable[[str, str], None]
"""(alert_text, idempotency_key) -> None — best-effort DM sink for drift alerts."""

logger = logging.getLogger(__name__)


kind_settling_seconds: dict[str, int] = {
    "slack_dm": 30,
    "slack_reaction": 30,
    "gitlab_note": 30,
    "gitlab_approve": 30,
    "notion_comment": 120,
    "notion_edit": 120,
}


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """The outcome of one verify-against-API call."""

    verified: bool
    drift_reason: str = ""

    @classmethod
    def ok(cls) -> "VerifyResult":
        return cls(verified=True)

    @classmethod
    def drift(cls, reason: str) -> "VerifyResult":
        return cls(verified=False, drift_reason=reason)


type Verifier = Callable[[OutboundClaimModel], VerifyResult]


def _default_notifier(alert_text: str, idempotency_key: str) -> None:
    """Production drift-notifier: post via the overlay bot, idempotent on key."""
    from teatree.notify import (  # noqa: PLC0415 — lazy keeps import side-effects out of module load
        NotifyKind,
        notify_user,
    )

    notify_user(alert_text, kind=NotifyKind.INFO, idempotency_key=idempotency_key)


@dataclass(slots=True)
class OutboundAuditScanner:
    """Verify each claimed outbound post against the third-party API.

    The *notifier* dependency is injected so tests can mock the DM sink.
    In production it defaults to :func:`_default_notifier` which calls
    :func:`teatree.notify.notify_user`. Pass ``notifier=lambda *_: None``
    (or any silent callable) to disable DMs while keeping drift detection.
    """

    verifiers: dict[str, Verifier] = field(default_factory=dict)
    notifier: "DriftNotifier" = field(default=_default_notifier)
    limit: int = 50
    name: str = "outbound_audit"
    _now_factory: Callable[[], dt.datetime] = field(default=timezone.now)

    DEFAULT_DRIFT_TEMPLATE: ClassVar[str] = (
        ":rotating_light: *outbound drift*\n"
        "Claim said I posted a *{kind}* artifact at <{url}|the target>; "
        "the third-party API does not show it.\n"
        "Diagnosis: {reason}"
    )

    def scan(self) -> list[ScanSignal]:
        model = cast("type[OutboundClaimModel]", apps.get_model("core", "OutboundClaim"))
        now = self._now_factory()
        signals: list[ScanSignal] = []
        candidates = self._candidate_claims(model, now)
        for claim in candidates:
            verifier = self.verifiers.get(claim.kind) or _default_verifier_for(claim.kind)
            if verifier is None:
                logger.debug("No verifier for kind=%s — skipping claim %s", claim.kind, claim.pk)
                continue
            try:
                result = verifier(claim)
            except Exception as exc:  # noqa: BLE001 — never break a tick on a verifier raise
                logger.warning("Verifier for %s raised: %s — skipping (no alert)", claim.kind, exc)
                continue
            signal = self._apply_result(claim, result, now)
            if signal is not None:
                signals.append(signal)
        return signals

    def _candidate_claims(
        self,
        model: "type[OutboundClaimModel]",
        now: dt.datetime,
    ) -> "list[OutboundClaimModel]":
        """Pull rows that are eligible for verification this tick.

        A row is eligible when:
        - it is not yet verified, AND
        - it has not yet been alerted as drift, AND
        - its ``claim_ts`` is older than the per-kind settling window, AND
        - its ``idempotency_key`` does not start with ``outbound_drift:``.

        The last condition is the recursion guard: the drift DM itself
        records an ``OutboundClaim`` row (any successful Slack post does).
        Without the prefix exclusion the scanner would re-verify those DM
        claims on the next tick and, if the drift DM itself failed to
        land, would emit *another* drift DM about the missing drift DM —
        a feedback loop. The fixed ``outbound_drift:`` prefix is wired by
        :meth:`_apply_result` below, so the contract is local.
        """
        rows = (
            model.objects.filter(
                verified_at__isnull=True,
                drift_alerted_at__isnull=True,
            )
            .filter(claim_ts__lte=now)
            .exclude(idempotency_key__startswith="outbound_drift:")
            .order_by("claim_ts")[: self.limit * 2]
        )
        eligible: list[OutboundClaimModel] = []
        for row in rows:
            window = kind_settling_seconds.get(row.kind, 30)
            if (now - row.claim_ts).total_seconds() >= window:
                eligible.append(row)
                if len(eligible) >= self.limit:
                    break
        return eligible

    def _apply_result(
        self,
        claim: "OutboundClaimModel",
        result: VerifyResult,
        now: dt.datetime,
    ) -> ScanSignal | None:
        if result.verified:
            claim.verified_at = now
            claim.save(update_fields=["verified_at"])
            return None

        claim.drift_detected = True
        claim.drift_reason = result.drift_reason
        claim.drift_alerted_at = now
        claim.save(update_fields=["drift_detected", "drift_reason", "drift_alerted_at"])
        alert_text = self.DEFAULT_DRIFT_TEMPLATE.format(
            kind=claim.kind,
            url=claim.target_url or "(no url)",
            reason=result.drift_reason or "artifact missing",
        )
        try:
            self.notifier(alert_text, f"outbound_drift:{claim.idempotency_key}")
        except Exception as exc:  # noqa: BLE001 — never break a tick on a notifier raise
            logger.warning("Drift notifier raised: %s — drift recorded but DM skipped", exc)
        return ScanSignal(
            kind="outbound.drift",
            summary=f"Drift on {claim.kind}: {result.drift_reason[:80]}",
            payload={
                "claim_id": claim.pk,
                "claim_kind": claim.kind,
                "target_url": claim.target_url,
                "drift_reason": result.drift_reason,
            },
        )


def _default_verifier_for(kind: str) -> Verifier | None:
    """Lazy production-default verifiers built from overlay factory clients.

    Returns ``None`` when no production verifier exists for the kind — the
    scanner then skips the row (no alert). Tests inject explicit verifiers
    via :class:`OutboundAuditScanner`'s ``verifiers`` dict, so the
    production path here is intentionally minimal and never raises.
    """
    if kind == "gitlab_note":
        return _gitlab_note_verifier()
    if kind == "gitlab_approve":
        return _gitlab_approve_verifier()
    if kind == "slack_dm":
        return _slack_dm_verifier()
    return None


def _gitlab_note_verifier() -> Verifier | None:
    """Build a GitLab-note verifier from the overlay's GitLab credentials."""
    try:
        from teatree.backends.gitlab_api import GitLabAPI  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        api = GitLabAPI()
    except Exception:  # noqa: BLE001
        return None

    def _verify(claim: "OutboundClaimModel") -> VerifyResult:
        repo = str(claim.extra.get("repo", ""))
        mr = claim.extra.get("mr")
        artifact_id = str(claim.extra.get("artifact_id", ""))
        endpoint = str(claim.extra.get("endpoint", "notes"))
        if not (repo and isinstance(mr, int) and artifact_id):
            return VerifyResult.ok()
        encoded = repo.replace("/", "%2F")
        sub = "draft_notes" if "draft_notes" in endpoint else "notes"
        if not artifact_id.isdigit():
            return VerifyResult.ok()
        try:
            api.get_json(f"projects/{encoded}/merge_requests/{mr}/{sub}/{artifact_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.NOT_FOUND:
                return VerifyResult.drift(f"GitLab note {artifact_id} not found on !{mr}")
            raise
        return VerifyResult.ok()

    return _verify


def _usernames_from_approvers(approved_by: list[object]) -> set[str]:
    """Extract the set of approving usernames from a GitLab approvals payload."""
    names: set[str] = set()
    for entry in approved_by:
        if not isinstance(entry, dict):
            continue
        user = cast("RawAPIDict", entry).get("user")
        if isinstance(user, dict):
            username = cast("RawAPIDict", user).get("username")
            if isinstance(username, str):
                names.add(username)
    return names


def _gitlab_approve_verifier() -> Verifier | None:
    """Build a GitLab-approval verifier from the overlay's GitLab credentials."""
    try:
        from teatree.backends.gitlab_api import GitLabAPI  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        api = GitLabAPI()
        my_username = api.current_username()
    except Exception:  # noqa: BLE001
        return None
    if not my_username:
        return None

    def _verify(claim: "OutboundClaimModel") -> VerifyResult:
        repo = str(claim.extra.get("repo", ""))
        mr = claim.extra.get("mr")
        endpoint = str(claim.extra.get("endpoint", "approve"))
        if not (repo and isinstance(mr, int)):
            return VerifyResult.ok()
        encoded = repo.replace("/", "%2F")
        try:
            approvals = api.get_json(f"projects/{encoded}/merge_requests/{mr}/approvals")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.NOT_FOUND:
                return VerifyResult.drift(f"GitLab MR !{mr} approvals endpoint 404")
            raise
        raw_approved = approvals.get("approved_by") if isinstance(approvals, dict) else None
        approved_by: list[object] = list(raw_approved) if isinstance(raw_approved, list) else []
        names = _usernames_from_approvers(approved_by)
        present = my_username in names
        if endpoint == "approve" and not present:
            return VerifyResult.drift(
                f"Approval by {my_username} not present on !{mr} (claimed approve)",
            )
        if endpoint == "unapprove" and present:
            return VerifyResult.drift(
                f"Approval by {my_username} still present on !{mr} (claimed unapprove)",
            )
        return VerifyResult.ok()

    return _verify


def _slack_dm_verifier() -> Verifier | None:
    """Build a Slack-DM verifier from the overlay messaging backend.

    Confirms via ``chat.getPermalink`` that the ``(channel, ts)`` recorded
    in the claim's ``extra`` still resolves to a permalink. Mirrors the
    GitLab verifier's error doctrine:

    - An empty permalink (the backend's "ok=false" / 404-equivalent
        return shape: ``channel_not_found`` / ``message_not_found``)
        → :class:`VerifyResult.drift` — the message did not land.
    - Any transport-level exception (``httpx.HTTPStatusError`` for HTTP
        5xx, ``httpx.NetworkError`` for connection failures, etc.)
        → re-raise. ``scan()`` catches and skips the row silently so we
        do not spam drift DMs on a temporary backend outage.
    """
    try:
        from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    backend = messaging_from_overlay()
    if backend is None:
        return None

    def _verify(claim: "OutboundClaimModel") -> VerifyResult:
        channel = str(claim.extra.get("channel", ""))
        ts = str(claim.extra.get("ts", ""))
        if not (channel and ts):
            return VerifyResult.ok()
        # Let httpx.* and any other transport-layer exception propagate
        # so ``scan()`` can skip the row silently — drift is reserved for
        # "the backend told us the artifact is gone", not "we could not
        # reach the backend".
        permalink = backend.get_permalink(channel=channel, ts=ts)
        if not permalink:
            return VerifyResult.drift(f"Slack message {ts} not found in {channel}")
        return VerifyResult.ok()

    return _verify


__all__ = [
    "DriftNotifier",
    "OutboundAuditScanner",
    "Verifier",
    "VerifyResult",
    "kind_settling_seconds",
]
