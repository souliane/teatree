"""Overlay-bound verifier factories for the outbound-audit scanner (#1275).

The outbound-audit scanner (``loop.scanners.outbound_audit``) routes each
claim through a verifier built with the SAME overlay's credentials that
posted it. This module owns the per-overlay factories so the scanner
file stays under the module-health LOC cap.

Failure mode this addresses: pre-#1275 the verifier factories called
``GitLabAPI()`` / ``messaging_from_overlay()`` / ``_resolve_github_token()``
with no overlay arg, so multi-overlay setups (one overlay with
``github_token_ref="github/work-token"``, another with
``github_token_ref="github/personal"``) silently picked whichever
credential the process-global resolver landed on. On a repo readable
only by the non-default token, the GitHub/GitLab API returned 404 →
false drift DM. This module fixes that by reading
``claim.extra["overlay"]`` and binding each verifier to that overlay's
own credential pipeline.

Legacy single-overlay factories live in :mod:`outbound_audit` and now
delegate here with ``overlay_name=""`` so the pre-#1275 contract still
holds for callers that don't record overlay context.
"""

from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import httpx

if TYPE_CHECKING:
    from teatree.core.models import OutboundClaim as OutboundClaimModel
    from teatree.loop.scanners.outbound_audit import Verifier
    from teatree.types import RawAPIDict


def slack_dm_verifier_for_overlay(overlay_name: str) -> "Verifier | None":
    """Build a Slack-DM verifier bound to a specific overlay's messaging backend.

    ``overlay_name`` is the value recorded on ``OutboundClaim.extra["overlay"]``
    at post time. Empty string is the canonical single-overlay default
    (the same key ``messaging_from_overlay`` uses when no overlay is
    explicitly selected).

    Mirrors the legacy verifier's error doctrine:

    - An empty permalink (the backend's "ok=false" / 404-equivalent
        return shape: ``channel_not_found`` / ``message_not_found``)
        → :class:`VerifyResult.drift` — the message did not land.
    - Any transport-level exception (``httpx.HTTPStatusError`` for HTTP
        5xx, ``httpx.NetworkError`` for connection failures, etc.)
        → re-raise. ``scan()`` catches and skips the row silently so we
        do not spam drift DMs on a temporary backend outage.

    Returns ``None`` (no verifier) when the overlay's messaging backend
    can't be constructed — the scanner emits ``outbound.audit_skipped``
    rather than treating that as drift.
    """
    from teatree.loop.scanners.outbound_audit import VerifyResult  # noqa: PLC0415

    try:
        from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    backend = messaging_from_overlay(overlay_name=overlay_name or None)
    if backend is None:
        return None

    def _verify(claim: "OutboundClaimModel") -> "VerifyResult":
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


def gitlab_api_for_overlay(overlay_name: str) -> object | None:
    """Build a ``GitLabAPI`` instance using the named overlay's credentials.

    Returns ``None`` when the overlay can't be resolved, its GitLab token
    doesn't resolve, or the ``GitLabAPI`` module isn't importable.
    Mirrors the legacy ``GitLabAPI()`` constructor's resolve-or-skip
    contract, but reads the token from the matching overlay rather than
    the process-global env/pass fallback.
    """
    try:
        from teatree.backends.gitlab.api import GitLabAPI  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    token, base_url = _overlay_gitlab_credentials(overlay_name)
    try:
        if token:
            return GitLabAPI(token=token, base_url=base_url)
        # Legacy fallback: empty overlay or unregistered overlay name —
        # use the process-global default resolver (same shape pre-#1275).
        return GitLabAPI()
    except Exception:  # noqa: BLE001
        return None


def _overlay_gitlab_credentials(overlay_name: str) -> tuple[str, str]:
    """Return ``(token, base_url)`` for the named overlay's GitLab config.

    Empty overlay name (or an unresolvable name) returns ``("", "")`` so
    the caller falls back to the legacy single-overlay default. Reading
    through the overlay config is the load-bearing change — a wrapper
    script that opts an overlay into a non-default ``gitlab_token_ref``
    now drives the audit verifier through THAT token.
    """
    if not overlay_name:
        return ("", "")
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ("", "")
    try:
        overlay = get_overlay(overlay_name)
    except Exception:  # noqa: BLE001
        # TOML overlay (no Python class) — try the raw config table.
        return _overlay_gitlab_credentials_from_toml(overlay_name)
    try:
        token = overlay.config.get_gitlab_token()
    except Exception:  # noqa: BLE001
        token = ""
    base_url = getattr(overlay.config, "gitlab_url", "https://gitlab.com/api/v4")
    return (token or "", base_url or "https://gitlab.com/api/v4")


def _overlay_gitlab_credentials_from_toml(overlay_name: str) -> tuple[str, str]:
    """Resolve a TOML-only overlay's ``gitlab_token_ref`` via ``pass``.

    Path-only overlays (no Python class, opted in via
    ``[overlays.<name>]`` in the DB overlays registry) keep their credentials
    in that config table, mirroring ``backend_factory._hosts_from_toml``.
    """
    try:
        from teatree.config import load_config  # noqa: PLC0415
        from teatree.utils.secrets import read_pass  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ("", "")
    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return ("", "")
    token_ref = str(cfg.get("gitlab_token_ref", ""))
    base_url = str(cfg.get("gitlab_url", "https://gitlab.com")).rstrip("/")
    if not token_ref:
        return ("", "")
    if not base_url.endswith("/api/v4"):
        base_url = f"{base_url}/api/v4"
    try:
        token = read_pass(token_ref)
    except Exception:  # noqa: BLE001
        token = ""
    return (token or "", base_url)


def gitlab_note_verifier_for_overlay(overlay_name: str) -> "Verifier | None":
    """Build a GitLab-note verifier from the named overlay's GitLab token."""
    from teatree.loop.scanners.outbound_audit import VerifyResult, _gitlab_api_for_overlay  # noqa: PLC0415

    api = _gitlab_api_for_overlay(overlay_name)
    if api is None:
        return None

    def _verify(claim: "OutboundClaimModel") -> "VerifyResult":
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
            api.get_json(f"projects/{encoded}/merge_requests/{mr}/{sub}/{artifact_id}")  # type: ignore[attr-defined]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.NOT_FOUND:
                return VerifyResult.drift(f"GitLab note {artifact_id} not found on !{mr}")
            raise
        return VerifyResult.ok()

    return _verify


def gitlab_approve_verifier_for_overlay(overlay_name: str) -> "Verifier | None":
    """Build a GitLab-approve verifier from the named overlay's credentials."""
    from teatree.loop.scanners.outbound_audit import (  # noqa: PLC0415
        VerifyResult,
        _gitlab_api_for_overlay,
        _usernames_from_approvers,
    )

    api = _gitlab_api_for_overlay(overlay_name)
    if api is None:
        return None
    try:
        my_username = api.current_username()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None
    if not my_username:
        return None

    def _verify(claim: "OutboundClaimModel") -> "VerifyResult":
        repo = str(claim.extra.get("repo", ""))
        mr = claim.extra.get("mr")
        endpoint = str(claim.extra.get("endpoint", "approve"))
        if not (repo and isinstance(mr, int)):
            return VerifyResult.ok()
        encoded = repo.replace("/", "%2F")
        try:
            approvals = api.get_json(f"projects/{encoded}/merge_requests/{mr}/approvals")  # type: ignore[attr-defined]
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


def resolve_github_token_for_overlay(overlay_name: str) -> str:
    """Resolve a GitHub PAT bound to ``overlay_name``'s config.

    Empty overlay name (or an unresolvable name) falls through to the
    legacy resolver (env → ``pass`` default keys). A registered overlay
    with its own ``github_token_ref`` (or ``github_pat`` getter) takes
    precedence — this is the credential pipeline that #1275 binds
    verifiers to.
    """
    from teatree.loop.scanners.outbound_audit import _resolve_github_token  # noqa: PLC0415

    if not overlay_name:
        return _resolve_github_token()
    token = _github_token_from_registered_overlay(overlay_name)
    if token:
        return token
    token = _github_token_from_toml_overlay(overlay_name)
    if token:
        return token
    return _resolve_github_token()


def _github_token_from_registered_overlay(overlay_name: str) -> str:
    """Read the GitHub token off a Python-class-registered overlay."""
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        overlay = get_overlay(overlay_name)
    except Exception:  # noqa: BLE001
        return ""
    try:
        return overlay.config.get_github_token() or ""
    except Exception:  # noqa: BLE001
        return ""


def _github_token_from_toml_overlay(overlay_name: str) -> str:
    """Read the GitHub token off a path-only TOML overlay.

    Mirrors ``backend_factory._hosts_from_toml`` so path-only overlays
    keep one credential pipeline shared with the loop's host scanners.
    """
    try:
        from teatree.config import load_config  # noqa: PLC0415
        from teatree.utils.secrets import read_pass  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    overlays = load_config().raw.get("overlays") or {}
    cfg = overlays.get(overlay_name)
    if not isinstance(cfg, dict):
        return ""
    token_ref = str(cfg.get("github_token_ref", ""))
    if not token_ref:
        return ""
    try:
        return read_pass(token_ref) or ""
    except Exception:  # noqa: BLE001
        return ""


def github_note_verifier_for_overlay(overlay_name: str) -> "Verifier | None":
    """Build a GitHub-note verifier with the named overlay's GitHub token.

    Mirrors the legacy verifier's error doctrine but resolves the token
    through ``overlay_name``'s credential pipeline so multi-overlay
    setups don't cross-talk. Empty-token → ``None`` and the scanner
    emits ``outbound.audit_skipped`` for the row (never drift — an
    unauthenticated 404 on a private repo is ambiguous and must not
    become a false alert).
    """
    from teatree.loop.scanners.outbound_audit import (  # noqa: PLC0415
        VerifyResult,
        _hash_body,
        _is_github_not_found,
        _resolve_github_token_for_overlay,
    )

    try:
        from teatree.backends.github.client import _gh_api_get  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    token = _resolve_github_token_for_overlay(overlay_name)
    if not token:
        return None

    def _verify(claim: "OutboundClaimModel") -> "VerifyResult":
        repo = str(claim.extra.get("repo", ""))
        artifact_id = str(claim.extra.get("artifact_id", ""))
        expected_digest = str(claim.extra.get("payload_digest", ""))
        if not (repo and artifact_id and artifact_id.isdigit()):
            return VerifyResult.ok()
        try:
            data = _gh_api_get(f"repos/{repo}/issues/comments/{artifact_id}", token=token)
        except Exception as exc:
            if _is_github_not_found(exc):
                return VerifyResult.drift(
                    f"GitHub comment {artifact_id} not found on {repo}",
                )
            raise
        if not isinstance(data, dict):
            return VerifyResult.drift(
                f"GitHub comment {artifact_id} on {repo} returned non-dict payload",
            )
        if expected_digest:
            actual_body = str(cast("RawAPIDict", data).get("body", ""))
            if _hash_body(actual_body) != expected_digest:
                return VerifyResult.drift(
                    f"GitHub comment {artifact_id} body digest mismatch on {repo}",
                )
        return VerifyResult.ok()

    return _verify


__all__ = [
    "github_note_verifier_for_overlay",
    "gitlab_api_for_overlay",
    "gitlab_approve_verifier_for_overlay",
    "gitlab_note_verifier_for_overlay",
    "resolve_github_token_for_overlay",
    "slack_dm_verifier_for_overlay",
]
