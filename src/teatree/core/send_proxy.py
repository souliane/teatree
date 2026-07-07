"""The single outbound chokepoint every artifact routes through (#117).

One send-proxy owns three concerns for every outbound artifact — a Slack
post/DM/react (:mod:`teatree.core.notify`, :class:`~teatree.core.on_behalf_egress.OnBehalfSlackEgress`,
:mod:`teatree.core.reply_transport`) and a forge PR/MR/issue comment
(:class:`teatree.cli.review.service.ReviewService`, the issue-filing path):

1.  **It is the ONLY module that reads a posting credential.** Every
    point-of-use secret-store read for a Slack bot/app/user token or a
    GitHub/GitLab forge token goes through :func:`read_posting_credential`,
    so the credential surface is one auditable function instead of scattered
    ``read_pass`` calls. (Pinned by the single-credential-reader grep-gate.)

2.  **It enforces a per-overlay destination allowlist** (Slack channels, forge
    repos, hosts). The allowlist is deterministic, so a BLOCK is legitimate —
    but it ships in ``warn`` mode (audit-only) so it can never over-block a
    real send before the operator seeds the allowlist from a live-traffic soak.

3.  **It runs redaction/banned-terms on every payload.** In ``warn`` mode the
    matches are audited but the live payload is never mutated; in ``enforce``
    mode the payload is redacted before the wire call.

Every send writes one :class:`~teatree.core.models.send_audit.SendAudit` row
carrying the delegation provenance (#119 reads it). The proxy is **fail-open in
``warn`` mode and never-raise in the audit path** — a policy-evaluation or
audit-write failure degrades to "allow, unredacted, unaudited" and is logged, so
the proxy can sit on the hot outbound path without ever breaking a send.

Home is :mod:`teatree.core`: it imports config, the overlay loader (for the
allowlist + redact terms), and the shared :mod:`teatree.hooks.term_match`
matcher — no edge into ``teatree.backends``. The ``SendAudit`` / ``Provenance``
model imports are DEFERRED into the functions that use them: this module sits on
the CLI bootstrap path (``teatree.cli.review.service`` imports it at module
scope, reached from ``teatree.cli`` before ``django.setup()``), so a top-level
``teatree.core.models.*`` import would drag the whole model registry in
pre-app-registry and raise ``ImproperlyConfigured``.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction

from teatree.config import get_effective_settings
from teatree.config.enums import SendProxyMode
from teatree.core.overlay_loader import get_overlay
from teatree.core.session_identity import current_session_id
from teatree.utils import secrets

if TYPE_CHECKING:
    # Annotation-only: keeps ``_audit_verdict``'s return type resolvable to the
    # ORM model without a runtime import on the pre-app-registry bootstrap path.
    from teatree.core.models.send_audit import SendAudit

logger = logging.getLogger(__name__)

#: Placeholder a redacted whole-token match is replaced with in ``enforce`` mode.
REDACTION_PLACEHOLDER = "[redacted]"

#: How much of the payload is kept in the audit row's non-sensitive preview.
_PAYLOAD_SUMMARY_CHARS = 200


def read_posting_credential(ref: str) -> str:
    """Read a *posting* credential (Slack/forge token) from the secret store.

    THE single point-of-use reader for every posting credential — the Slack
    bot/app/user token and the GitHub/GitLab forge token. Consolidating the
    reads here (instead of scattered ``read_pass`` call sites) makes the
    credential surface one auditable function; the single-credential-reader
    grep-gate pins that no other module reads a posting credential directly.

    A blank *ref* short-circuits to ``""`` (no store call), matching the
    old inline ``read_pass(ref) if ref else ""`` guards at the call sites.

    The underlying :func:`teatree.utils.secrets.read_pass` is reached through
    the module (``secrets.read_pass``), not a bound import, so
    ``patch("teatree.utils.secrets.read_pass", …)`` stays the one stub point for
    the whole posting-credential path.
    """
    if not ref:
        return ""
    return secrets.read_pass(ref)


class SendChannel(StrEnum):
    """The outbound surface a send targets — routes the audit + visibility probe."""

    SLACK = "slack"
    GITHUB = "github"
    GITLAB = "gitlab"
    OTHER = "other"


def _default_provenance() -> str:
    """The default ``SendRequest.provenance`` — ``Provenance.OWNER`` (the operator's own send).

    A ``default_factory`` (not a class-body constant) so the ``Provenance``
    import stays deferred: the enum lives in the ``teatree.core.models`` package,
    whose ``__init__`` eagerly loads the ORM model registry, and evaluating it at
    class-definition time would break the pre-app-registry CLI bootstrap path.
    """
    from teatree.core.models.provenance import Provenance  # noqa: PLC0415 — deferred: ORM model pkg, pre-app-registry

    return Provenance.OWNER.value


@dataclass(frozen=True, slots=True)
class SendRequest:
    """One outbound artifact presented to the proxy for policy + audit.

    ``payload`` is the body/text about to egress. ``destination`` is the raw
    surface id the allowlist matches (Slack channel id, ``org/repo`` slug, forge
    host). ``target`` is the human-facing artifact ref for the audit
    (``org/repo#42``). ``authorized_by`` / ``provenance`` are the delegation
    fields #119 consumes — which directive/ticket/human sanctioned the send and
    the trust origin of its content.
    """

    channel: SendChannel
    destination: str
    payload: str
    action: str
    target: str = ""
    overlay: str = ""
    authorized_by: str = ""
    provenance: str = field(default_factory=_default_provenance)
    is_self_dm: bool = False


@dataclass(frozen=True, slots=True)
class SendVerdict:
    """The proxy's decision for one send.

    ``allowed`` is always ``True`` in ``warn`` mode (audit-only). ``payload`` is
    the original body in ``warn`` mode and the redacted body in ``enforce`` mode.
    ``allowlist_ok`` reports the raw allowlist check regardless of mode (so the
    audit and a caller can see the would-be verdict during the soak).
    """

    allowed: bool
    payload: str
    mode: SendProxyMode
    allowlist_ok: bool
    redaction_matches: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def payload_redacted(self) -> bool:
        """True when redaction actually mutated the payload (matches present AND enforce mode)."""
        return bool(self.redaction_matches) and self.mode is SendProxyMode.ENFORCE


def _resolve_mode(overlay: str) -> SendProxyMode:
    """The effective ``send_proxy_mode`` for *overlay* (env → DB(overlay) → DB(global) → WARN)."""
    try:
        return get_effective_settings(overlay or None).send_proxy_mode
    except Exception as exc:  # noqa: BLE001 — a settings failure must fail SAFE (warn), never enforce.
        logger.debug("send_proxy: mode resolution failed for overlay=%r (%s) — defaulting to warn", overlay, exc)
        return SendProxyMode.WARN


def _allowlist(overlay: str) -> list[str]:
    try:
        return list(get_effective_settings(overlay or None).send_proxy_allowlist)
    except Exception as exc:  # noqa: BLE001 — an unreadable allowlist is empty, not fatal.
        logger.debug("send_proxy: allowlist resolution failed for overlay=%r (%s)", overlay, exc)
        return []


def destination_allowed(channel: SendChannel, destination: str, *, overlay: str, is_self_dm: bool = False) -> bool:
    """Whether *destination* is on *overlay*'s send-proxy allowlist.

    The user's own DM (``is_self_dm``) is ALWAYS allowed — the never-lockout
    carve-out so a mis-seeded allowlist can never gate the bot→user notify path.
    Otherwise a destination matches when a ``send_proxy_allowlist`` glob matches
    either the bare ``destination`` or the channel-qualified
    ``<channel>:<destination>`` form (``fnmatch``, mirroring ``clean_ignore``).
    An empty allowlist matches nothing — in ``enforce`` mode that denies every
    non-self destination, which is why the flip only happens after a soak.
    """
    if is_self_dm:
        return True
    patterns = _allowlist(overlay)
    if not patterns:
        return False
    qualified = f"{channel.value}:{destination}"
    return any(fnmatch(destination, pat) or fnmatch(qualified, pat) for pat in patterns)


def _redact_terms(overlay: str) -> list[str]:
    """The active overlay's ``privacy_redact_terms`` — the redaction vocabulary.

    Best-effort: when no overlay resolves the list is empty and redaction is a
    no-op (the allowlist is still evaluated). Reuses the same per-overlay term
    set the #1295 publication privacy gate scans, so redaction and the leak gate
    never drift.
    """
    try:
        return list(get_overlay().config.privacy_redact_terms)
    except Exception as exc:  # noqa: BLE001 — overlay redact rules are a best-effort add.
        logger.debug("send_proxy: redact-term resolution failed for overlay=%r (%s)", overlay, exc)
        return []


def redact_payload(payload: str, *, overlay: str) -> tuple[str, tuple[str, ...]]:
    """Return ``(redacted_payload, matched_terms)`` for *payload*.

    Each WHOLE-TOKEN occurrence of an overlay ``privacy_redact_terms`` entry is
    replaced with :data:`REDACTION_PLACEHOLDER`. Whole-token means the term is
    not preceded or followed by a word character — so a short term never
    surfaces inside a longer word (``op`` inside ``cooperative`` is left alone),
    the same anti-substring property the #1295 leak gate enforces. Matching is
    case-insensitive. ``matched_terms`` is the sorted set of terms that actually
    fired (empty when the payload is clean), so a caller can tell a redacted
    payload from an untouched one.
    """
    terms = _redact_terms(overlay)
    if not payload or not terms:
        return payload, ()
    matched: set[str] = set()
    redacted = payload
    for term in terms:
        if not term:
            continue
        # ``(?<!\w)…(?!\w)`` is a whole-token boundary robust to a term that
        # ends in a non-word char (where ``\b`` misbehaves).
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        if pattern.search(redacted):
            matched.add(term)
            redacted = pattern.sub(REDACTION_PLACEHOLDER, redacted)
    return redacted, tuple(sorted(matched))


def route_send(request: SendRequest) -> SendVerdict:
    """Evaluate one outbound send: allowlist + redaction + audit.

    The single policy chokepoint. In ``warn`` mode (ship default) it is
    audit-only — ``allowed`` is ``True``, the live payload is returned unchanged,
    and a :class:`SendAudit` row records the would-be allowlist verdict and the
    redaction matches. In ``enforce`` mode a non-allowlisted destination yields
    ``allowed=False`` and the returned payload is redacted.

    Never raises out of the audit path: a policy-evaluation or audit-write
    failure degrades to allow/unredacted/unaudited (logged), so the proxy on the
    hot outbound path can never break a send.
    """
    mode = _resolve_mode(request.overlay)
    allowlist_ok = _safe_allowlist_check(request)
    redacted, matches = _safe_redact(request)

    if mode is SendProxyMode.ENFORCE:
        allowed = allowlist_ok
        out_payload = redacted
        reason = "" if allowed else f"destination {request.destination!r} not on the send-proxy allowlist"
    else:  # WARN — audit-only, never block, never mutate the live payload.
        allowed = True
        out_payload = request.payload
        reason = ""

    verdict = SendVerdict(
        allowed=allowed,
        payload=out_payload,
        mode=mode,
        allowlist_ok=allowlist_ok,
        redaction_matches=matches,
        reason=reason,
    )
    _record_audit(request, verdict)
    return verdict


def _safe_allowlist_check(request: SendRequest) -> bool:
    try:
        return destination_allowed(
            request.channel,
            request.destination,
            overlay=request.overlay,
            is_self_dm=request.is_self_dm,
        )
    except Exception as exc:  # noqa: BLE001 — a check failure fails SAFE (allowed), never a spurious block.
        logger.debug("send_proxy: allowlist check failed for %s (%s) — treating as allowed", request.destination, exc)
        return True


def _safe_redact(request: SendRequest) -> tuple[str, tuple[str, ...]]:
    try:
        return redact_payload(request.payload, overlay=request.overlay)
    except Exception as exc:  # noqa: BLE001 — redaction is best-effort; a failure leaves the payload unchanged.
        logger.debug("send_proxy: redaction failed (%s) — leaving payload unredacted", exc)
        return request.payload, ()


def _record_audit(request: SendRequest, verdict: SendVerdict) -> None:
    """Write the per-send :class:`SendAudit` row — best-effort, never raises.

    Fields are pre-truncated to their column widths here (SQLite does not enforce
    ``max_length``, and a Postgres backend would otherwise raise on an overlong
    destination/target) so a pathological ref can never break the audit write.
    """
    from teatree.core.models.send_audit import SendAudit  # noqa: PLC0415 — deferred: ORM model, pre-app-registry

    overlay = request.overlay or os.environ.get("T3_OVERLAY_NAME", "") or ""
    try:
        with transaction.atomic():
            SendAudit.objects.create(
                channel=request.channel.value,
                destination=request.destination[:512],
                action=request.action[:64],
                target=request.target[:512],
                overlay=overlay[:255],
                mode=verdict.mode.value,
                allowlist_verdict=_audit_verdict(verdict),
                redaction_applied=verdict.payload_redacted,
                redaction_matches=list(verdict.redaction_matches),
                provenance=request.provenance[:32],
                authorized_by=request.authorized_by[:255],
                agent_session_id=(current_session_id() or "")[:255],
                payload_summary=request.payload[:_PAYLOAD_SUMMARY_CHARS],
            )
    except DatabaseError as exc:
        logger.warning("send_proxy: audit write failed for destination=%s: %s", request.destination, exc)
    except Exception as exc:  # noqa: BLE001 — the audit is a side ledger; never break the send.
        logger.debug("send_proxy: audit record failed for destination=%s: %s", request.destination, exc)


def _audit_verdict(verdict: SendVerdict) -> "SendAudit.Verdict":
    """Map a :class:`SendVerdict` onto the audit's tri-state verdict enum."""
    from teatree.core.models.send_audit import SendAudit  # noqa: PLC0415 — deferred: ORM model, pre-app-registry

    if verdict.allowlist_ok:
        return SendAudit.Verdict.ALLOWED
    if verdict.mode is SendProxyMode.ENFORCE:
        return SendAudit.Verdict.DENIED
    return SendAudit.Verdict.WARNED


class SendBlockedError(RuntimeError):
    """An ``enforce``-mode send was refused — the destination is not allowlisted.

    Raised by a chokepoint that opts to hard-fail on a blocked verdict (the
    on-behalf Slack egress, a forge comment). Callers that instead degrade to a
    no-op / FAILED row inspect ``verdict.allowed`` directly. Only reachable in
    ``enforce`` mode — inert on the ``warn`` ship default.
    """

    def __init__(self, verdict: SendVerdict) -> None:
        super().__init__(verdict.reason or "send-proxy refused the destination")
        self.verdict = verdict


__all__ = [
    "REDACTION_PLACEHOLDER",
    "SendBlockedError",
    "SendChannel",
    "SendRequest",
    "SendVerdict",
    "destination_allowed",
    "read_posting_credential",
    "redact_payload",
    "route_send",
]
