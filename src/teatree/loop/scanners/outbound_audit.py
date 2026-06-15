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
from typing import TYPE_CHECKING, ClassVar, cast

from django.apps import apps
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

# Overlay-aware verifier factories — defined in a sibling module so this
# file stays under the module-health LOC cap. Re-exported here as
# module-level names because the test suite patches them via the
# ``teatree.loop.scanners.outbound_audit.<name>`` path, and the sibling
# module's lazy ``from outbound_audit import _xxx`` imports therefore see
# the patched values (#1275). The sibling defers its ``VerifyResult`` /
# private-helper imports back to this module to avoid a cycle at import time.
from teatree.loop.scanners.outbound_audit_overlay_verifiers import (
    github_note_verifier_for_overlay as _github_note_verifier_for_overlay,
)
from teatree.loop.scanners.outbound_audit_overlay_verifiers import (
    gitlab_api_for_overlay as _gitlab_api_for_overlay,  # noqa: F401 — re-exported for test patching
)
from teatree.loop.scanners.outbound_audit_overlay_verifiers import (
    gitlab_approve_verifier_for_overlay as _gitlab_approve_verifier_for_overlay,
)
from teatree.loop.scanners.outbound_audit_overlay_verifiers import (
    gitlab_note_verifier_for_overlay as _gitlab_note_verifier_for_overlay,
)
from teatree.loop.scanners.outbound_audit_overlay_verifiers import (
    resolve_github_token_for_overlay as _resolve_github_token_for_overlay,  # noqa: F401 — re-exported for test patching
)
from teatree.loop.scanners.outbound_audit_overlay_verifiers import (
    slack_dm_verifier_for_overlay as _slack_dm_verifier_for_overlay,
)

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
    "github_note": 30,
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


def _noop_notifier(alert_text: str, idempotency_key: str) -> None:
    """Silent drift sink — the default when no notifier is injected.

    Drift is still detected and recorded on the row; only the DM is
    suppressed. The production notifier (``messaging``/``notify`` egress)
    lives at the orchestration construction site
    (:func:`teatree.loop.domain_jobs.default_drift_notifier`) and is injected,
    so this scanner stays in the ``domain`` layer with no ``integration``
    up-edge.
    """


@dataclass(slots=True)
class OutboundAuditScanner:
    """Verify each claimed outbound post against the third-party API.

    The *notifier* dependency is injected so tests can mock the DM sink and
    the loop construction site (:func:`domain_jobs._global_dispatch_jobs`)
    wires the production
    :func:`teatree.loop.domain_jobs.default_drift_notifier`. Defaults to
    :func:`_noop_notifier` (drift recorded, no DM) so a bare construction
    never reaches into the ``messaging``/``notify`` integration layer.
    """

    verifiers: dict[str, Verifier] = field(default_factory=dict)
    notifier: "DriftNotifier" = field(default=_noop_notifier)
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
            # Explicit injected verifier wins (test path); else resolve the
            # production verifier bound to the overlay that posted the
            # claim (#1275). Per-claim resolution is the load-bearing
            # change: the same scanner instance now picks up the right
            # backend for each row, not whichever credential a single
            # global resolver landed on at scanner construction.
            verifier = self.verifiers.get(claim.kind) or _default_verifier_for_claim(claim)
            if verifier is None:
                signals.append(_audit_skipped_signal(claim))
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
        claim.save(update_fields=["drift_detected", "drift_reason"])
        alert_text = self.DEFAULT_DRIFT_TEMPLATE.format(
            kind=claim.kind,
            url=claim.target_url or "(no url)",
            reason=result.drift_reason or "artifact missing",
        )
        try:
            self.notifier(alert_text, f"outbound_drift:{claim.idempotency_key}")
        except Exception as exc:  # noqa: BLE001 — never break a tick on a notifier raise
            logger.warning("Drift notifier raised: %s — drift recorded, alert retried next tick", exc)
        else:
            claim.drift_alerted_at = now
            claim.save(update_fields=["drift_alerted_at"])
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
    """Lazy production-default verifiers built from the default overlay.

    Returns ``None`` when no production verifier exists for the kind — the
    scanner then skips the row (no alert). Kept as the legacy single-
    overlay entry point and exercised by the dispatcher tests; per-claim
    overlay-bound resolution is :func:`_default_verifier_for_claim`.
    """
    if kind == "gitlab_note":
        return _gitlab_note_verifier()
    if kind == "gitlab_approve":
        return _gitlab_approve_verifier()
    if kind == "github_note":
        return _github_note_verifier()
    if kind == "slack_dm":
        return _slack_dm_verifier()
    return None


def _default_verifier_for_claim(claim: "OutboundClaimModel") -> Verifier | None:
    """Build a production verifier bound to the overlay that posted the claim.

    Reads ``claim.extra["overlay"]`` and constructs the right backend
    from THAT overlay's credentials (#1275). When the overlay name is
    absent (legacy rows recorded before the overlay-stamping change), or
    the credential pipeline for that overlay returns nothing, the result
    is ``None`` — the scanner then emits ``outbound.audit_skipped`` so
    the silent backlog is observable, never re-classified as drift.
    """
    overlay = str(claim.extra.get("overlay", ""))
    kind = claim.kind
    if kind == "slack_dm":
        return _slack_dm_verifier_for_overlay(overlay)
    if kind == "gitlab_note":
        return _gitlab_note_verifier_for_overlay(overlay)
    if kind == "gitlab_approve":
        return _gitlab_approve_verifier_for_overlay(overlay)
    if kind == "github_note":
        return _github_note_verifier_for_overlay(overlay)
    return None


def _audit_skipped_signal(claim: "OutboundClaimModel") -> ScanSignal:
    """Build an ``outbound.audit_skipped`` ScanSignal for an unverifiable claim.

    Emitted when no verifier resolves for a claim's (kind, overlay) pair
    — the credential is missing for the recorded overlay. Distinct from
    ``outbound.drift`` so the dispatcher and statusline can surface the
    backlog separately rather than mis-classifying a credential gap as
    a missing artifact (#1275).
    """
    overlay = str(claim.extra.get("overlay", ""))
    return ScanSignal(
        kind="outbound.audit_skipped",
        summary=f"No verifier for {claim.kind} overlay={overlay or '<default>'}",
        payload={
            "claim_id": claim.pk,
            "claim_kind": claim.kind,
            "overlay": overlay,
            "target_url": claim.target_url,
        },
    )


def _gitlab_note_verifier() -> Verifier | None:
    """Legacy single-overlay GitLab-note verifier — delegates to the overlay-aware sibling.

    Kept so existing patch-based tests pinning the factory's import-
    guard and constructor-raise paths keep working. Production code
    paths go through :func:`_default_verifier_for_claim` (#1275).
    """
    return _gitlab_note_verifier_for_overlay("")


def _resolve_github_token() -> str:
    """Resolve a GitHub PAT from env, falling back to the ``pass`` store.

    Mirrors :func:`teatree.backends.gitlab.api._resolve_token` so the
    GitHub-note verifier has the same credential pipeline the GitLab
    verifier already relies on. An empty result means the verifier
    factory will return ``None`` (no production verifier) and the
    scanner skips ``github_note`` rows silently — never spam drift on a
    credential gap (which on private repos would otherwise surface as a
    404 indistinguishable from a missing comment).
    """
    import os  # noqa: PLC0415

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if token:
        return token
    try:
        from teatree.utils.secrets import read_pass  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    try:
        return read_pass("github/token") or read_pass("github/pat") or ""
    except Exception:  # noqa: BLE001
        return ""


def _github_note_verifier() -> Verifier | None:
    """Legacy single-overlay GitHub-note verifier — delegates to the overlay-aware sibling.

    Kept so existing patch-based tests pinning the factory's
    import-guard, missing-token, and verifier-behaviour paths keep
    working. Production code paths go through
    :func:`_default_verifier_for_claim` (#1275).
    """
    return _github_note_verifier_for_overlay("")


def _is_github_not_found(exc: BaseException) -> bool:
    """``gh api`` surfaces HTTP 404 in the CommandFailedError's stderr.

    The ``gh`` CLI exits non-zero on 404 and prints ``HTTP 404: Not Found``
    to stderr (the exact phrasing has been stable since ``gh`` 2.x). We
    detect by substring match — looser than a strict regex but robust to
    the small wording variations ``gh`` has shipped over the years.

    NOTE: an *unauthenticated* call to a private repo also surfaces as
    404 to mask resource existence; that's why the factory rejects an
    empty token up front rather than running with no auth. Once the
    token is established, a 404 is meaningfully a missing-comment signal.
    """
    stderr = getattr(exc, "stderr", "") or ""
    return "HTTP 404" in stderr or "404 Not Found" in stderr


def _hash_body(body: str) -> str:
    """SHA-256 digest of the comment body, hex-encoded.

    Used as the claim's ``payload_digest`` so the verifier can detect
    silent body-divergence (e.g. a server-side edit between POST and
    verify) without storing the full body in the claim row.
    """
    import hashlib  # noqa: PLC0415 — stdlib, cheap, only used here

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
    """Legacy single-overlay GitLab-approve verifier — delegates to the overlay-aware sibling."""
    return _gitlab_approve_verifier_for_overlay("")


def _slack_dm_verifier() -> Verifier | None:
    """Build a Slack-DM verifier from the default overlay's messaging backend.

    Legacy single-overlay entry. The overlay-bound sibling
    :func:`_slack_dm_verifier_for_overlay` (#1275) supersedes this on the
    per-claim path; tests still construct this directly to exercise the
    underlying verifier behaviour.
    """
    return _slack_dm_verifier_for_overlay("")


__all__ = [
    "DriftNotifier",
    "OutboundAuditScanner",
    "Verifier",
    "VerifyResult",
    "_hash_body",
    "kind_settling_seconds",
]
