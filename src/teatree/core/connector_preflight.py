"""Loop-start connector preflight gate.

A loop tick that proceeds with a down connector (Slack ``missing_scope``,
Notion unreachable, claude.ai connector offline) does not error — it
silently no-ops: scanners find nothing, ``notify_user`` records a phantom
``SENT``, the user is told nothing. The user directive is the opposite:
*refuse to continue* when a hard-dependency connector is unreachable.

This module runs every registered overlay's
:meth:`OverlayBase.get_connector_preflight` callables before any
connector-dependent loop work. The first ``RuntimeError`` is fatal —
``run_connector_preflight`` ``raise SystemExit(1)`` with a message naming
which connector is down, following teatree's management-command exit
convention (``raise SystemExit``, never ``typer.Exit``).
"""

import logging

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.connector_keys import GRANTED_SCOPES_KEY
from teatree.core.connector_manifest import OverlayManifest, check_connector_manifest
from teatree.core.mcp_connectivity import McpProbe
from teatree.core.overlay_loader import get_all_overlays

logger = logging.getLogger(__name__)


def assert_slack_scope(backend: MessagingBackend, scope: str) -> None:
    """Raise ``RuntimeError`` when *backend*'s token lacks *scope*.

    Reads the granted OAuth scopes that ``auth.test`` surfaces from the
    ``X-OAuth-Scopes`` response header (under
    :data:`~teatree.core.connector_keys.GRANTED_SCOPES_KEY`). An overlay's
    connector-preflight callable wires this so the loop refuses to continue
    when a required scope (e.g. ``reactions:write``) is missing — rather than
    discovering ``missing_scope`` mid-tick via a phantom write success.
    """
    response = backend.auth_test()
    if not response.get("ok"):
        error = response.get("error", "unknown error")
        msg = f"Slack auth.test failed: {error}"
        raise RuntimeError(msg)
    raw = response.get(GRANTED_SCOPES_KEY)
    granted = [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
    if scope not in granted:
        msg = (
            f"Slack token is missing the {scope!r} scope "
            f"(granted: {', '.join(granted) or 'none'}). "
            "Re-run `t3 setup slack-user-token` after updating the app manifest."
        )
        raise RuntimeError(msg)


def assert_required_connectors(manifests: list[OverlayManifest], *, probe: McpProbe | None = None) -> None:
    """Raise ``RuntimeError`` naming each declared REQUIRED connector that is down (PR-19).

    The manifest counterpart to :func:`assert_slack_scope` at loop start: a
    claude.ai connector an overlay hard-depends on that is not connected refuses
    the loop rather than letting it degrade into silent no-ops. A degraded probe
    (``claude`` absent) does NOT block — :func:`check_connector_manifest` cannot
    prove a disconnection, so the loop proceeds and the doctor gate surfaces the
    WARN separately. An empty manifest is a no-op.
    """
    outcome = check_connector_manifest(manifests=manifests, probe=probe)
    if outcome.required_findings:
        joined = "; ".join(outcome.required_findings)
        msg = f"required claude.ai connector(s) not connected: {joined}"
        raise RuntimeError(msg)


def run_connector_preflight(overlay_name: str = "") -> None:
    """Run connector probes for every registered overlay (or one named).

    Each overlay contributes zero or more zero-arg callables via
    :meth:`OverlayBase.get_connector_preflight`, plus its declared connector
    manifest (:meth:`OverlayBase.get_connector_manifest`). A callable that raises
    ``RuntimeError`` — or a required declared connector that is down — means a
    hard-dependency connector is unreachable; this function then
    ``raise SystemExit(1)`` so the loop/lifecycle entrypoint refuses to continue
    rather than degrade into silent no-ops. A clean run returns ``None``.
    """
    overlays = get_all_overlays()
    if overlay_name:
        selected = {overlay_name: overlays[overlay_name]} if overlay_name in overlays else {}
    else:
        selected = overlays

    manifests: list[OverlayManifest] = []
    for name, overlay in selected.items():
        for check in overlay.get_connector_preflight():
            try:
                check()
            except RuntimeError as exc:
                msg = (
                    f"Connector preflight failed for overlay {name!r}: {exc}. "
                    "Refusing to continue — fix the connector and retry."
                )
                logger.exception(msg)
                # ``SystemExit`` with a str arg: Python prints it to
                # stderr and exits 1. ``.code`` is the message (truthy →
                # non-zero exit). Follows teatree's mgmt-command exit
                # convention (raise SystemExit, never typer.Exit).
                raise SystemExit(msg) from exc
        manifests.append(OverlayManifest(overlay=name, requirements=list(overlay.get_connector_manifest())))

    try:
        assert_required_connectors(manifests)
    except RuntimeError as exc:
        msg = f"Connector preflight failed: {exc}. Refusing to continue — reconnect the connector and retry."
        logger.exception(msg)
        raise SystemExit(msg) from exc


__all__ = ["assert_required_connectors", "assert_slack_scope", "run_connector_preflight"]
