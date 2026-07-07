"""Enabled-MCP connectivity + declared-provider verification (souliane/teatree#2282).

An MCP server the operator has enabled but whose live connection is broken is a
silent failure: tool calls against it fail late, mid-task, with no obvious root
cause. This module is the single chokepoint that enumerates every *enabled*
configured MCP server, live-probes each one's *connected* status, and validates
each server resolves to its overlay-*declared* provider (claude.ai-hosted vs a
third-party provider). The same :func:`check_mcp_connectivity` is called at
session start (the ``SessionStart`` hook advisory), in ``t3 doctor check``, on
the account-switch recovery path, and from the ``t3:setup`` skill flow — one
function, one set of findings, no per-surface drift.

Two seams keep the policy testable without the network. The reader
:func:`read_enabled_mcp_servers` is a pure, Django-free parser of
``~/.claude.json`` (top-level + project-scoped ``mcpServers`` plus the
claude.ai-hosted connectors in ``claudeAiMcpEverConnected``, minus the
per-project ``disabledMcpServers`` set), mirroring
:mod:`teatree.core.account_fingerprint`'s reader posture. The ``probe`` callable
runs ``claude mcp list`` (the harness's own live health check); the default
production probe is :func:`probe_mcp_servers`, and the unit test injects a stub
so the check runs with no subprocess and no network.

The overlay declares the EXPECTED provider per server name via
``OverlayBase.connectors.mcp_provider_expectations()`` (default ``{}``). Teatree's own
default is empty; the real per-server values live in the overlay repo
(souliane/teatree#251) — this module supplies only the validation logic and the
extension point.
"""

import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

CLAUDE_AI_HOSTED = "claude.ai-hosted"
THIRD_PARTY = "third-party"

_CLAUDE_AI_PREFIX = "claude.ai "
_CONNECTED_MARKER = "✔"
_MCP_LIST_TIMEOUT_SECONDS = 30
_CLAUDE_AI_HOSTED_HOSTS = ("mcp.notion.com", "mcp.slack.com", "mcp.sentry.dev")

_PROBE_FAILED_FINDING = (
    "Could not live-probe MCP connectivity ({detail}); run `claude mcp list` "
    "manually to verify enabled servers are connected."
)
_DISCONNECTED_FINDING = (
    "MCP server '{name}' is enabled but NOT connected. Reconnect it: run "
    "`claude mcp list` to confirm, then re-auth the connector in the Claude.ai "
    "UI (or restart its local command)."
)
_PROVIDER_MISMATCH_FINDING = (
    "MCP server '{name}' resolves to provider '{actual}' but the overlay expects "
    "'{expected}'. Verify the connector points at the declared provider."
)

type McpProbe = Callable[[], list[McpServerStatus]]


@dataclass(frozen=True, slots=True)
class ConfiguredMcpServer:
    """One enabled, configured MCP server and its resolved provider."""

    name: str
    provider: str


@dataclass(frozen=True, slots=True)
class McpServerStatus:
    """One server's live connectivity, parsed from ``claude mcp list``."""

    name: str
    url: str
    connected: bool


@dataclass(frozen=True, slots=True)
class McpConnectivityOutcome:
    """The result of one connectivity + provider check.

    ``ok`` is ``True`` only when every enabled server is connected and resolves
    to its declared provider. ``degraded`` is ``True`` when the live probe could
    not run (``claude`` absent, subprocess error) — the check then surfaces a
    WARN and never claims a disconnection it cannot prove.
    """

    ok: bool
    degraded: bool = False
    findings: list[str] = field(default_factory=list)


def resolve_provider(name: str, *, url: str) -> str:
    """Classify a server as claude.ai-hosted or third-party from its name/URL.

    A claude.ai connector is named ``claude.ai <Service>`` in ``~/.claude.json``
    and served from a ``mcp.<host>`` Anthropic-hosted endpoint; everything else
    (a local stdio command, a self-hosted HTTP server) is third-party.
    """
    if name.startswith(_CLAUDE_AI_PREFIX):
        return CLAUDE_AI_HOSTED
    if any(host in url for host in _CLAUDE_AI_HOSTED_HOSTS):
        return CLAUDE_AI_HOSTED
    return THIRD_PARTY


def _project(data: dict, cwd: Path) -> dict:
    """The ``projects`` entry for *cwd*, resolved by nearest-ancestor key.

    Claude keys ``projects`` by absolute path. A worktree cwd is rarely an exact
    key (the teatree model is worktree-first), so the entry is the one whose path
    is the longest ancestor of (or equal to) *cwd* — the same longest-prefix
    resolution Claude uses. An exact-``get`` would resolve the disabled set empty
    from a worktree and flag healthy-but-disabled servers as disconnected.
    """
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return {}
    candidates = [cwd, *cwd.parents]
    for ancestor in candidates:
        project = projects.get(str(ancestor))
        if isinstance(project, dict):
            return project
    return {}


def _project_disabled_servers(data: dict, cwd: Path) -> set[str]:
    disabled = _project(data, cwd).get("disabledMcpServers")
    return set(disabled) if isinstance(disabled, list) else set()


def _str_value(mapping: dict, key: str) -> str:
    """The ``key`` value of *mapping* as a string, ``""`` when absent or non-string."""
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _collect_server_providers(servers: object, into: dict[str, str]) -> None:
    """Resolve each ``{name: cfg}`` server's provider into *into* (first-wins)."""
    if not isinstance(servers, dict):
        return
    for raw_name, cfg in servers.items():
        if isinstance(raw_name, str):
            url = _str_value(cfg, "url") if isinstance(cfg, dict) else ""
            into.setdefault(raw_name, resolve_provider(raw_name, url=url))


def read_enabled_mcp_servers(*, home: Path | None = None, cwd: Path | None = None) -> list[ConfiguredMcpServer]:
    """Every enabled configured MCP server, with its resolved provider.

    Enabled = configured (top-level + project-scoped ``mcpServers`` + the
    claude.ai-hosted connectors in ``claudeAiMcpEverConnected``) minus the
    per-project ``disabledMcpServers`` set for *cwd*. A missing or malformed
    ``~/.claude.json`` is "no servers" (``[]``), never an error.
    """
    home = home if home is not None else Path.home()
    cwd = cwd if cwd is not None else Path.cwd()
    config_path = home / ".claude.json"
    if not config_path.is_file():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    disabled = _project_disabled_servers(data, cwd)
    configured: dict[str, str] = {}
    _collect_server_providers(data.get("mcpServers"), configured)
    _collect_server_providers(_project(data, cwd).get("mcpServers"), configured)

    ever_connected = data.get("claudeAiMcpEverConnected")
    if isinstance(ever_connected, list):
        for name in ever_connected:
            if isinstance(name, str):
                configured.setdefault(name, resolve_provider(name, url=""))

    return [
        ConfiguredMcpServer(name=name, provider=provider)
        for name, provider in configured.items()
        if name not in disabled
    ]


def read_ever_connected(*, home: Path | None = None) -> set[str]:
    """The set of claude.ai connector names that have EVER connected on this machine.

    Reads ``claudeAiMcpEverConnected`` from ``~/.claude.json`` (network-free,
    error-tolerant). This is the ground-truth signal that distinguishes the two
    connector-failure modes the recovery guidance branches on (PR-19): a declared
    connector that is NOT in this set has never been connected → a first-install
    case (add it in claude.ai Settings → Connectors); one that IS in the set but
    is now down → a post-account-switch case (reconnect it). A missing or
    malformed config is an empty set, never an error.
    """
    home = home if home is not None else Path.home()
    config_path = home / ".claude.json"
    if not config_path.is_file():
        return set()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    ever = data.get("claudeAiMcpEverConnected")
    return {name for name in ever if isinstance(name, str)} if isinstance(ever, list) else set()


def has_enabled_mcp_servers(*, home: Path | None = None, cwd: Path | None = None) -> bool:
    """Whether any MCP server is enabled — a cheap, network-free ``~/.claude.json`` read.

    The ``SessionStart`` hook's hot path uses this (not the live probe) to decide
    whether to surface the run-the-check advisory: a probe would exceed the 3s
    hook budget, so session start only nudges the agent to run ``t3 doctor check``
    (which does the bounded live probe) when there is something to check.
    """
    return bool(read_enabled_mcp_servers(home=home, cwd=cwd))


def parse_mcp_list_output(text: str) -> list[McpServerStatus]:
    """Parse ``claude mcp list`` text into per-server connectivity.

    Each server line is ``<name>: <url-or-transport> - <status>``. Connected is
    anchored on the ✔ glyph (``✔ Connected``) so it cannot substring-match a
    ``Not Connected`` / ``✘ Failed`` / ``⏸ Pending approval`` status — those and
    any other status are not connected.
    """
    statuses: list[McpServerStatus] = []
    for raw in text.splitlines():
        line = raw.strip()
        if ": " not in line or " - " not in line:
            continue
        name, _, rest = line.partition(": ")
        target, _, status = rest.rpartition(" - ")
        url = target.strip() if target.strip().startswith("http") else ""
        connected = _CONNECTED_MARKER in status
        statuses.append(McpServerStatus(name=name.strip(), url=url, connected=connected))
    return statuses


def probe_mcp_servers() -> list[McpServerStatus]:
    """Live-probe every MCP server via ``claude mcp list`` (the production seam).

    Runs the harness's own health check and parses its output. Raises when
    ``claude`` is absent or the subprocess fails — :func:`check_mcp_connectivity`
    catches that and degrades to a WARN rather than crashing the caller.
    """
    binary = shutil.which("claude")
    if binary is None:
        message = "claude binary not on PATH"
        raise FileNotFoundError(message)
    completed = run_allowed_to_fail(
        [binary, "mcp", "list"],
        expected_codes=None,
        timeout=_MCP_LIST_TIMEOUT_SECONDS,
    )
    return parse_mcp_list_output(completed.stdout)


def overlay_provider_expectations() -> dict[str, str]:
    """The merged ``{server_name: expected_provider}`` map from every overlay.

    Each registered overlay declares its expectations via
    ``OverlayBase.connectors.mcp_provider_expectations()``. Teatree's own default is
    empty; the real values live in the overlay repo (souliane/teatree#251).
    """
    from teatree.core.backend_factory import iter_overlay_backends  # noqa: PLC0415

    merged: dict[str, str] = {}
    for backend in iter_overlay_backends():
        overlay = getattr(backend, "overlay", None)
        if overlay is None:
            continue
        try:
            expectations = overlay.connectors.mcp_provider_expectations()
        except Exception:
            logger.debug("overlay %s raised in connectors.mcp_provider_expectations", overlay, exc_info=True)
            continue
        if isinstance(expectations, dict):
            merged.update(expectations)
    return merged


def check_mcp_connectivity(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    probe: McpProbe | None = None,
    provider_expectations: dict[str, str] | None = None,
) -> McpConnectivityOutcome:
    """Verify every enabled MCP server is connected and matches its declared provider.

    Produces a loud, actionable finding (naming the server + a reconnect hint)
    for every enabled server that is disconnected or absent from the live probe,
    and for every server whose resolved provider differs from the overlay-declared
    expectation. A probe that cannot run degrades to a WARN (``degraded=True``,
    ``ok=True``) — the check never claims a disconnection it cannot prove.
    """
    probe = probe if probe is not None else probe_mcp_servers
    expectations = provider_expectations if provider_expectations is not None else overlay_provider_expectations()

    enabled = read_enabled_mcp_servers(home=home, cwd=cwd)
    if not enabled:
        return McpConnectivityOutcome(ok=True)

    try:
        statuses = probe()
    except Exception as exc:  # noqa: BLE001 — any probe failure degrades, never crashes
        detail = f"{type(exc).__name__}: {exc}"
        return McpConnectivityOutcome(ok=True, degraded=True, findings=[_PROBE_FAILED_FINDING.format(detail=detail)])

    connected = {status.name for status in statuses if status.connected}
    findings: list[str] = []
    for server in enabled:
        if server.name not in connected:
            findings.append(_DISCONNECTED_FINDING.format(name=server.name))
        expected = expectations.get(server.name)
        if expected is not None and expected != server.provider:
            findings.append(
                _PROVIDER_MISMATCH_FINDING.format(name=server.name, actual=server.provider, expected=expected),
            )

    return McpConnectivityOutcome(ok=not findings, findings=findings)


__all__ = [
    "CLAUDE_AI_HOSTED",
    "THIRD_PARTY",
    "ConfiguredMcpServer",
    "McpConnectivityOutcome",
    "McpProbe",
    "McpServerStatus",
    "check_mcp_connectivity",
    "has_enabled_mcp_servers",
    "overlay_provider_expectations",
    "parse_mcp_list_output",
    "probe_mcp_servers",
    "read_enabled_mcp_servers",
    "read_ever_connected",
    "resolve_provider",
]
