"""``t3 doctor`` — WARN when ``autoload`` is off yet Slack posting is configured (#256).

With engagement default-OFF (``autoload`` off, #256) a session never engages
teatree, so it never routes Slack through the MCP tools — a bot configured with a
``slack_token_ref`` then reacts/posts nothing through the agent surface even though
the credentials are present. This surfacing-only check WARNs the moment those two
facts coexist and names the one-line fix. Slack stays optional: with no
Slack-configured overlay it is a silent no-op.
"""

from collections.abc import Callable
from dataclasses import dataclass

import typer

from teatree.cli.slack.socket_doctor import Level


@dataclass(frozen=True, slots=True)
class EngagementFinding:
    """One Slack-engagement observation the doctor renders on its own line."""

    level: Level
    message: str


def evaluate_slack_engagement(*, autoload: bool, slack_overlays: list[str]) -> EngagementFinding | None:
    """WARN when ``autoload`` is off yet Slack posting is configured; ``None`` otherwise.

    Pure decision so the two facts — engagement off, Slack configured — can be
    asserted directly. ``autoload`` on, or no Slack-configured overlay, is a clean
    no-op.
    """
    if autoload or not slack_overlays:
        return None
    names = ", ".join(sorted(slack_overlays))
    return EngagementFinding(
        Level.WARN,
        f"`autoload` is OFF but Slack posting is configured ({names}) — with engagement default-off "
        "(#256) sessions never engage teatree, so Slack never routes through the MCP tools and the "
        "bot reacts/posts nothing through the agent. Enable engagement: `config_setting set autoload true`.",
    )


def _slack_configured_overlays() -> list[str]:
    """Overlays declaring the Slack backend WITH a ``slack_token_ref`` posting entry."""
    from teatree.cli.slack.app_resolve import read_overlay_field  # noqa: PLC0415 — deferred: ORM-backed registry read
    from teatree.cli.slack.provision import _slack_overlays  # noqa: PLC0415 — deferred: ORM-backed registry read

    return [overlay for overlay in _slack_overlays() if read_overlay_field(overlay, "slack_token_ref")]


def check_slack_engagement(*, echo: Callable[[str], object] = typer.echo) -> bool:
    """Render the engagement WARN if it applies; always returns ``True`` (surfacing-only).

    Never gates the doctor exit code — Slack is optional and ``autoload`` off is a
    legitimate colleague/opted-out posture; the WARN only flags the *combination*
    where a configured bot is silently inert.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        autoload = bool(get_effective_settings().autoload)
        overlays = _slack_configured_overlays()
    except Exception:  # noqa: BLE001 — a doctor check must never crash the run
        return True
    finding = evaluate_slack_engagement(autoload=autoload, slack_overlays=overlays)
    if finding is not None:
        echo(f"{finding.level.value:<5} Slack engagement: {finding.message}")
    return True


__all__ = ["EngagementFinding", "check_slack_engagement", "evaluate_slack_engagement"]
