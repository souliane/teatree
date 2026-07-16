"""``t3 doctor`` Slack DM-readiness — fail-loud diagnosis per Slack overlay.

Consolidates the exact gaps a DM-only bot hits at runtime: an overlay declaring
``messaging_backend = "slack"`` that still cannot message or read its owner.
For every Slack-backed overlay in the DB ``overlays`` registry this reports:

1. FAIL when the overlay resolves to a :class:`NoopMessagingBackend` (or no
    backend at all) despite ``messaging_backend = "slack"`` — the bot tokens are
    missing at the ``slack_token_ref`` ``pass`` entry.
2. FAIL when ``slack_user_id`` is empty — the runtime ``SlackBotBackend`` can
    neither DM nor read its owner.
3. WARN when ``slack_dm_channel_id`` is empty — the per-overlay IM has not been
    provisioned yet (``t3 setup`` opens it).
4. OK when the backend resolves, the owner id is recorded, and the DM channel id
    is cached.

The doctor renderer (``_doctor_checks._check_slack_dm_ready``) consumes the
structured :class:`DmReadinessOutcome` this returns; it is surfacing-only, so a
FAIL here never gates the overall doctor exit code (Slack is optional).
"""

from dataclasses import dataclass

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.cli.slack.app_resolve import read_overlay_field
from teatree.cli.slack.dm_provisioning import SLACK_DM_CHANNEL_KEY
from teatree.cli.slack.provision import _slack_overlays
from teatree.cli.slack.socket_doctor import Level
from teatree.core.backend_factory import messaging_from_overlay


@dataclass(frozen=True, slots=True)
class DmReadinessFinding:
    """One overlay-scoped DM-readiness observation the doctor renders on its own line."""

    overlay: str
    level: Level
    message: str


@dataclass(frozen=True, slots=True)
class DmReadinessOutcome:
    """The full set of DM-readiness findings across every Slack overlay."""

    findings: tuple[DmReadinessFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(finding.level is Level.FAIL for finding in self.findings)


def _check_overlay_dm_readiness(overlay: str) -> list[DmReadinessFinding]:
    backend = messaging_from_overlay(overlay)
    if backend is None or isinstance(backend, NoopMessagingBackend):
        return [
            DmReadinessFinding(
                overlay,
                Level.FAIL,
                "resolves to a no-op messaging backend despite messaging_backend=slack — bot tokens missing at the "
                "`slack_token_ref` pass entry; run `t3 setup slack-bot`.",
            )
        ]

    findings: list[DmReadinessFinding] = []
    if not read_overlay_field(overlay, "slack_user_id"):
        findings.append(
            DmReadinessFinding(
                overlay,
                Level.FAIL,
                "no slack_user_id — the bot cannot DM or read its owner; set `pass slack/user-id` and re-run "
                "`t3 setup`.",
            )
        )
    if not read_overlay_field(overlay, SLACK_DM_CHANNEL_KEY):
        findings.append(DmReadinessFinding(overlay, Level.WARN, "DM channel not provisioned yet (run `t3 setup`)."))
    if not findings:
        findings.append(
            DmReadinessFinding(overlay, Level.OK, "Slack DM-ready — backend resolved, owner id + DM channel set.")
        )
    return findings


def check_slack_dm_ready() -> DmReadinessOutcome:
    """Diagnose DM-readiness for every Slack overlay in the DB ``overlays`` registry.

    An install with no Slack-backed overlay (the common case) yields no findings.
    One overlay's crash degrades to a single WARN so the rest still report.
    """
    findings: list[DmReadinessFinding] = []
    for overlay in _slack_overlays():
        try:
            findings.extend(_check_overlay_dm_readiness(overlay))
        except Exception as exc:  # noqa: BLE001 — one overlay's failure must not abort the rest
            findings.append(
                DmReadinessFinding(overlay, Level.WARN, f"DM-readiness check crashed: {exc.__class__.__name__}: {exc}")
            )
    return DmReadinessOutcome(findings=tuple(findings))


__all__ = [
    "DmReadinessFinding",
    "DmReadinessOutcome",
    "check_slack_dm_ready",
]
