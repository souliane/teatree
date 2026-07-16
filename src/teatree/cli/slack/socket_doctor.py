"""``t3 doctor`` Socket Mode readiness — validate + auto-fix per Slack overlay.

Extends teatree's existing Slack auto-management (``t3 setup slack-provision``
already pushes bot/user scopes via the app-config token) to cover Socket Mode
(BLUEPRINT § B5 / issue #106). For every Slack-backed overlay in the config this:

1. Validates the app-level ``xapp-`` token in the ``<token_ref>-app`` slot —
    present, correctly prefixed, and carrying ``connections:write`` (probed via
    ``apps.connections.open``). Slack has no API to mint an app-level token, so an
    absent one is the single actionable human step, surfaced with its exact
    Basic-Information URL and ``pass`` slot.
2. Auto-fixes the manifest where the Slack API allows — enables Socket Mode and
    adds the inbound events / bot scopes via ``apps.manifest.update`` using the
    app-config token. With no app-config token it degrades to an actionable
    message naming the manifest editor and the precise gaps.

The doctor renderer (``_doctor_checks._check_slack_socket_mode``) consumes the
structured :class:`SocketModeOutcome` this returns. Every live Slack call is
funnelled through a patchable module-level name so tests never touch the network.
"""

from dataclasses import dataclass
from enum import Enum

import httpx

from teatree.backends.slack.socket_mode import (
    DM_ONLY_SOCKET_BOT_SCOPES,
    DM_ONLY_SOCKET_EVENTS,
    SOCKET_MODE_APP_SCOPE,
    AppTokenProbe,
    ManifestSocketGaps,
    manifest_socket_gaps,
    probe_app_connections,
)
from teatree.backends.slack.token_validation import APP_TOKEN_RE
from teatree.cli.slack.app_resolve import overlay_scope_profile, read_overlay_field, resolve_overlay_app_id
from teatree.cli.slack.manifest import (
    _CONFIG_TOKEN_REF,
    SlackManifestError,
    app_install_url,
    app_level_token_url,
    app_manifest_editor_url,
    build_manifest,
    update_manifest,
)
from teatree.cli.slack.provision import _export_with_rotation, _slack_overlays
from teatree.cli.slack.token_store import app_token_slot
from teatree.utils.secrets import read_pass


class Level(Enum):
    """Severity of a Socket Mode finding, mapped to the doctor's line prefixes."""

    OK = "OK"
    ACTION = "ACTION"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class SocketModeFinding:
    """One overlay-scoped Socket Mode observation the doctor renders on its own line."""

    overlay: str
    level: Level
    message: str


@dataclass(frozen=True, slots=True)
class SocketModeOutcome:
    """The full set of Socket Mode findings across every Slack overlay."""

    findings: tuple[SocketModeFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(finding.level is Level.FAIL for finding in self.findings)


def _mint_app_token_message(app_id: str, pass_key: str) -> str:
    return (
        f"no app-level (xapp-) token for Socket Mode. Slack cannot mint one via API — "
        f"create it once at {app_level_token_url(app_id)} (App-Level Tokens → Generate, "
        f"scope {SOCKET_MODE_APP_SCOPE}, name 'teatree'), then store it: `pass insert {pass_key}`."
    )


def _check_app_token(overlay: str) -> list[SocketModeFinding]:
    token_ref = read_overlay_field(overlay, "slack_token_ref")
    app_id = resolve_overlay_app_id(overlay)
    if not token_ref:
        return [SocketModeFinding(overlay, Level.WARN, "no slack_token_ref configured — run `t3 setup slack-bot`.")]
    slot = app_token_slot(token_ref)
    app_token = read_pass(slot.pass_key)
    if not app_token:
        return [SocketModeFinding(overlay, Level.ACTION, _mint_app_token_message(app_id, slot.pass_key))]
    if not APP_TOKEN_RE.match(app_token):
        return [
            SocketModeFinding(
                overlay,
                Level.FAIL,
                f"app-level token in `pass {slot.pass_key}` is not an `xapp-` token — mint one at "
                f"{app_level_token_url(app_id)} (scope {SOCKET_MODE_APP_SCOPE}) and re-store it.",
            )
        ]
    try:
        probe = probe_app_connections(app_token)
    except httpx.HTTPError as exc:
        return [SocketModeFinding(overlay, Level.WARN, f"could not reach Slack to verify the app-level token: {exc}.")]
    return [_probe_finding(overlay, probe, app_id=app_id, pass_key=slot.pass_key)]


def _probe_finding(overlay: str, probe: AppTokenProbe, *, app_id: str, pass_key: str) -> SocketModeFinding:
    if probe.ok:
        return SocketModeFinding(
            overlay, Level.OK, f"app-level token valid — Socket Mode ready ({SOCKET_MODE_APP_SCOPE} granted)."
        )
    if probe.missing_scope:
        return SocketModeFinding(
            overlay,
            Level.FAIL,
            f"app-level token lacks {SOCKET_MODE_APP_SCOPE} — mint a new one at "
            f"{app_level_token_url(app_id)} and store it: `pass insert {pass_key}`.",
        )
    return SocketModeFinding(
        overlay, Level.WARN, f"could not verify the app-level token via apps.connections.open: {probe.error}."
    )


def _fixed_message(gaps: ManifestSocketGaps, app_id: str) -> str:
    parts: list[str] = []
    if gaps.socket_mode_disabled:
        parts.append("enabled Socket Mode")
    if gaps.missing_events:
        parts.append(f"added events {', '.join(sorted(gaps.missing_events))}")
    if gaps.missing_bot_scopes:
        parts.append(f"added bot scopes {', '.join(sorted(gaps.missing_bot_scopes))}")
    return f"Fixed manifest — {'; '.join(parts)}. Reinstall to consent: {app_install_url(app_id)}."


def _no_config_token_message(app_id: str, gaps: ManifestSocketGaps) -> str:
    missing: list[str] = []
    if gaps.socket_mode_disabled:
        missing.append("Socket Mode disabled")
    if gaps.missing_events:
        missing.append(f"events {', '.join(sorted(gaps.missing_events))}")
    if gaps.missing_bot_scopes:
        missing.append(f"bot scopes {', '.join(sorted(gaps.missing_bot_scopes))}")
    docs_url = "https://api.slack.com/reference/manifests#config_tokens"
    return (
        f"manifest Socket Mode gaps ({'; '.join(missing)}) but no app-config token in "
        f"`pass {_CONFIG_TOKEN_REF}` to auto-fix. Add one (create at {docs_url}): "
        f"`pass insert {_CONFIG_TOKEN_REF}`, or edit the manifest manually at {app_manifest_editor_url(app_id)}."
    )


def _check_manifest(overlay: str) -> list[SocketModeFinding]:
    app_id = resolve_overlay_app_id(overlay)
    if not app_id:
        message = "no slack_app_id — cannot inspect the manifest; run `t3 setup slack-bot`."
        return [SocketModeFinding(overlay, Level.WARN, message)]
    try:
        current = _export_with_rotation(app_id=app_id)
    except SlackManifestError as exc:
        return [SocketModeFinding(overlay, Level.WARN, f"could not export the manifest: {exc}.")]
    profile = overlay_scope_profile(overlay)
    if profile == "dm_only":
        gaps = manifest_socket_gaps(
            current, required_events=DM_ONLY_SOCKET_EVENTS, required_bot_scopes=DM_ONLY_SOCKET_BOT_SCOPES
        )
    else:
        gaps = manifest_socket_gaps(current)
    if gaps.ok:
        message = "manifest Socket Mode config current (socket mode on, events + scopes present)."
        return [SocketModeFinding(overlay, Level.OK, message)]
    if not read_pass(_CONFIG_TOKEN_REF):
        return [SocketModeFinding(overlay, Level.ACTION, _no_config_token_message(app_id, gaps))]
    desired = build_manifest(overlay_name=overlay, scope_profile=profile)
    try:
        update_manifest(app_id=app_id, manifest=desired, config_token=read_pass(_CONFIG_TOKEN_REF))
    except SlackManifestError as exc:
        return [SocketModeFinding(overlay, Level.WARN, f"manifest update failed: {exc}.")]
    return [SocketModeFinding(overlay, Level.OK, _fixed_message(gaps, app_id))]


def check_slack_socket_mode() -> SocketModeOutcome:
    """Validate + auto-fix Socket Mode readiness for every Slack overlay.

    Sources the Slack-backed overlays from the DB ``overlays`` registry — an
    install with none (the common case) yields no findings and no live calls.
    """
    findings: list[SocketModeFinding] = []
    for overlay in _slack_overlays():
        try:
            findings.extend(_check_app_token(overlay))
            findings.extend(_check_manifest(overlay))
        except Exception as exc:  # noqa: BLE001 — one overlay's failure must not abort the rest
            findings.append(
                SocketModeFinding(overlay, Level.WARN, f"Socket Mode check crashed: {exc.__class__.__name__}: {exc}")
            )
    return SocketModeOutcome(findings=tuple(findings))


__all__ = [
    "Level",
    "SocketModeFinding",
    "SocketModeOutcome",
    "check_slack_socket_mode",
]
