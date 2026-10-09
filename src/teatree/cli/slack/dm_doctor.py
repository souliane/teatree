"""``t3 doctor`` Slack DM CONFIG completeness — per-overlay, resolved BY NAME.

Scope, deliberately narrow: this answers "is each Slack overlay's DM config filled
in", NOT "does a notification actually reach the owner". It resolves each overlay
BY NAME, while the headless egress resolves ambiently with no ``T3_OVERLAY_NAME``
— so a complete per-overlay config here is fully compatible with a dead runtime
transport. Deliverability belongs to
:mod:`teatree.cli.doctor.checks_slack_roundtrip`, which probes the ambient seam and
reads the ``BotPing`` ledger back. The OK line here must therefore never be worded
so an operator can read it as "notifications work".

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
4. OK when the by-name backend resolves, the owner id is recorded, and the DM
    channel id is cached — CONFIG COMPLETE, not "DMs deliver".

The doctor renderer (:func:`check_and_render_dm_ready`) consumes the structured
:class:`DmReadinessOutcome` this returns; it is surfacing-only, so a FAIL here
never gates the overall doctor exit code (Slack is optional).
"""

from collections.abc import Callable
from dataclasses import dataclass

import typer

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
            DmReadinessFinding(
                overlay,
                Level.OK,
                "Slack DM config complete (backend resolved by name, owner id + DM channel set) — config only, "
                "NOT proof a DM delivers; the Slack round-trip gate owns deliverability.",
            )
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


def check_and_render_dm_ready(echo: Callable[[str], object] = typer.echo) -> bool:
    """Report Slack DM CONFIG completeness per overlay — fail-loud diagnosis of config gaps.

    For every overlay declaring ``messaging_backend = "slack"`` it surfaces the
    exact gaps that leave a DM-only bot unable to message or read its owner: a
    no-op backend (bot tokens missing), an empty ``slack_user_id``, or an
    unprovisioned DM channel. Consumes the structured :class:`DmReadinessOutcome`.
    An all-OK verdict means the config is filled in, never that a DM delivers —
    see this module's docstring for why the two are not the same question.

    Surfacing-only: always returns ``True`` so it never gates the overall doctor
    exit code (Slack is optional — it must never become mandatory). Crash-proof:
    any error degrades to a WARN so a doctor run never aborts on this check.
    """
    try:
        outcome = check_slack_dm_ready()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        echo(f"WARN  Slack DM-readiness check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for finding in outcome.findings:
        echo(f"{finding.level.value:<5} [{finding.overlay}] {finding.message}")
    return True


__all__ = [
    "DmReadinessFinding",
    "DmReadinessOutcome",
    "check_and_render_dm_ready",
    "check_slack_dm_ready",
]
