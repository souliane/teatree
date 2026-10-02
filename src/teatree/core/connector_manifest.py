"""Per-overlay claude.ai connector manifest + reconnect guidance (PR-19).

An overlay hard-depends on some claude.ai-hosted MCP connectors (e.g. Slack,
Notion) and merely benefits from others. Today a down connector is a
silent mid-tick no-op with no single place that says "this connector is required
and it is not connected". This module is that place: each overlay declares its
required-vs-optional connectors by NAME (:class:`ConnectorRequirement` on
``OverlayConnectors.manifest``), and :func:`check_connector_manifest`
reads the same enabled/connected ground truth the #2282 connectivity probe uses
(``~/.claude.json`` + ``claude mcp list``) to produce mode-correct findings and
``RECONNECT`` lines.

The failure mode is classified deterministically from
``claudeAiMcpEverConnected`` (:func:`teatree.core.mcp_connectivity.read_ever_connected`):
a declared connector that has NEVER connected is a first-install case (add it in
claude.ai Settings → Connectors under the active account); one that HAS connected
but is now down is a post-account-switch case (reconnect it). A required down
connector fails the check; an optional one only warns.
"""

import logging
from dataclasses import dataclass, field

from teatree.core.mcp_connectivity import McpProbe, probe_mcp_servers, read_ever_connected
from teatree.core.overlay_loader import get_all_overlays

logger = logging.getLogger(__name__)

CONNECTORS_SETTINGS_URL = "https://claude.ai/settings/connectors"

_FIRST_INSTALL_FINDING = (
    "connector {name!r} (required by overlay {overlay!r}) has never connected — "
    "add it in claude.ai Settings → Connectors under the active account, then re-run."
)
_RECONNECT_FINDING = (
    "connector {name!r} (required by overlay {overlay!r}) is down — reconnect it (`t3 mcp reconnect`), then re-run."
)
_OPTIONAL_FINDING = (
    "optional connector {name!r} (overlay {overlay!r}) is not connected — features "
    "that need it will surface a single actionable error until it is reconnected."
)
_PROBE_FAILED_FINDING = (
    "Could not live-probe connector connectivity ({detail}); run `claude mcp list` "
    "manually to verify the declared connectors are connected."
)


@dataclass(frozen=True, slots=True)
class ConnectorRequirement:
    """One overlay-declared claude.ai connector requirement.

    ``name`` matches the MCP server name in ``~/.claude.json`` /
    ``claude mcp list`` (e.g. ``"claude.ai Slack"``). ``required`` gates the
    check verdict (a down required connector FAILs; an optional one WARNs).
    ``instruction`` overrides the default reconnect line for a connector that is
    not reconnected through the settings page (a bespoke re-auth flow an overlay
    declares for itself).
    """

    name: str
    required: bool = True
    reconnect_url: str = CONNECTORS_SETTINGS_URL
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class DownConnector:
    """A declared connector resolved as not-connected, with its failure mode."""

    requirement: ConnectorRequirement
    overlay: str
    ever_connected: bool

    def reconnect_line(self) -> str:
        """One ``RECONNECT <name> -> <target>`` line for the recovery surfaces."""
        req = self.requirement
        target = req.instruction or req.reconnect_url
        return f"RECONNECT {req.name} -> {target}"


@dataclass(frozen=True, slots=True)
class ConnectorManifestOutcome:
    """The result of one manifest check.

    ``ok`` is ``True`` when no REQUIRED connector is down (an optional down
    connector warns but does not fail). ``degraded`` is ``True`` when the live
    probe could not run — the check then WARNs and never claims a disconnection
    it cannot prove, mirroring
    :class:`teatree.core.mcp_connectivity.McpConnectivityOutcome`.
    """

    ok: bool
    degraded: bool = False
    down: list[DownConnector] = field(default_factory=list)
    required_findings: list[str] = field(default_factory=list)
    optional_findings: list[str] = field(default_factory=list)
    probe_findings: list[str] = field(default_factory=list)

    def reconnect_lines(self) -> list[str]:
        """One ``RECONNECT`` line per down connector, required first then optional."""
        ordered = sorted(self.down, key=lambda d: (not d.requirement.required, d.requirement.name))
        return [d.reconnect_line() for d in ordered]


@dataclass(frozen=True, slots=True)
class OverlayManifest:
    """One overlay's declared connector requirements, keyed by overlay name."""

    overlay: str
    requirements: list[ConnectorRequirement]


def overlay_connector_manifests() -> list[OverlayManifest]:
    """Every registered overlay's declared connector manifest (possibly empty).

    Mirrors :func:`teatree.core.connector_preflight.run_connector_preflight`'s
    overlay walk. Every registered overlay contributes an entry — even when its
    manifest is empty — so "declared for every registered overlay" is structurally
    true (the acceptance requirement). An overlay whose hook raises is skipped
    with a debug log, never crashing the caller.
    """
    manifests: list[OverlayManifest] = []
    for name, overlay in get_all_overlays().items():
        try:
            requirements = overlay.connectors.manifest()
        except Exception:
            logger.debug("overlay %s raised in connectors.manifest", name, exc_info=True)
            requirements = []
        manifests.append(OverlayManifest(overlay=name, requirements=list(requirements)))
    return manifests


def check_connector_manifest(
    *,
    manifests: list[OverlayManifest] | None = None,
    probe: McpProbe | None = None,
    ever_connected: set[str] | None = None,
) -> ConnectorManifestOutcome:
    """Verify every declared connector is connected, classifying each failure mode.

    Reads the enabled+connected ground truth (``~/.claude.json`` +
    ``claude mcp list``) and, for each declared connector that is not connected,
    emits a finding whose text depends on whether the connector has EVER
    connected (first-install vs post-account-switch) and whether it is required
    (FAIL) or optional (WARN). A probe that cannot run degrades to a WARN.
    """
    manifests = manifests if manifests is not None else overlay_connector_manifests()
    requirements = [(m.overlay, req) for m in manifests for req in m.requirements]
    if not requirements:
        return ConnectorManifestOutcome(ok=True)

    try:
        statuses = (probe if probe is not None else probe_mcp_servers)()
    except Exception as exc:  # noqa: BLE001 — any probe failure degrades, never crashes
        detail = f"{type(exc).__name__}: {exc}"
        finding = _PROBE_FAILED_FINDING.format(detail=detail)
        return ConnectorManifestOutcome(ok=True, degraded=True, probe_findings=[finding])

    connected = {status.name for status in statuses if status.connected}
    ever = ever_connected if ever_connected is not None else read_ever_connected()

    down: list[DownConnector] = []
    required_findings: list[str] = []
    optional_findings: list[str] = []
    for overlay, req in requirements:
        if req.name in connected:
            continue
        was_connected = req.name in ever
        down.append(DownConnector(requirement=req, overlay=overlay, ever_connected=was_connected))
        if req.required:
            template = _RECONNECT_FINDING if was_connected else _FIRST_INSTALL_FINDING
            required_findings.append(template.format(name=req.name, overlay=overlay))
        else:
            optional_findings.append(_OPTIONAL_FINDING.format(name=req.name, overlay=overlay))

    return ConnectorManifestOutcome(
        ok=not required_findings,
        down=down,
        required_findings=required_findings,
        optional_findings=optional_findings,
    )


class ConnectorUnavailableError(RuntimeError):
    """A feature needs a declared connector that is not connected (PR-19 item 5).

    Raised by :func:`require_connector` so a connector-dependent feature surfaces
    exactly ONE actionable error pointing at the doctor guidance, instead of a
    silent no-op or a raw deep-in-the-stack transport error.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"connector {name!r} is not connected. Run `t3 doctor check` for the "
            f"mode-correct guidance, or `t3 mcp reconnect` to reconnect it.",
        )


def require_connector(
    name: str,
    *,
    probe: McpProbe | None = None,
) -> None:
    """Raise :class:`ConnectorUnavailableError` when connector *name* is not connected.

    The graceful-degradation seam: a feature that hard-depends on a claude.ai
    connector calls this before using it, so an absent connector produces one
    actionable error pointing at the doctor guidance rather than a silent
    no-op. A probe that cannot run does NOT raise (fail-open) — the feature then
    surfaces its own downstream error rather than being blocked on an
    unverifiable connectivity claim.
    """
    try:
        statuses = (probe if probe is not None else probe_mcp_servers)()
    except Exception:
        logger.debug("require_connector could not probe for %r; failing open", name, exc_info=True)
        return
    connected = {status.name for status in statuses if status.connected}
    if name not in connected:
        raise ConnectorUnavailableError(name)


__all__ = [
    "CONNECTORS_SETTINGS_URL",
    "ConnectorManifestOutcome",
    "ConnectorRequirement",
    "ConnectorUnavailableError",
    "DownConnector",
    "OverlayManifest",
    "check_connector_manifest",
    "overlay_connector_manifests",
    "require_connector",
]
