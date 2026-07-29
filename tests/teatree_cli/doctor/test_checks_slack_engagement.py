"""``t3 doctor`` WARNs when ``autoload`` is off yet Slack posting is configured (#256).

With engagement default-off a session never engages teatree, so a bot configured
with a ``slack_token_ref`` never routes Slack through the MCP tools. The check
fires ONLY on that combination — ``autoload`` on, or no Slack-configured overlay,
is a clean no-op (anti-vacuous: the WARN must not fire when either fact is absent).
"""

from dataclasses import dataclass

import pytest

from teatree.cli.doctor.checks_slack_engagement import check_slack_engagement, evaluate_slack_engagement
from teatree.cli.slack.socket_doctor import Level

_SETTINGS = "teatree.config.get_effective_settings"
_OVERLAYS = "teatree.cli.doctor.checks_slack_engagement._slack_configured_overlays"


@dataclass(frozen=True, slots=True)
class _FakeSettings:
    autoload: bool


def test_warns_when_autoload_off_and_slack_configured() -> None:
    finding = evaluate_slack_engagement(autoload=False, slack_overlays=["acme"])
    assert finding is not None
    assert finding.level is Level.WARN
    assert "config_setting set autoload true" in finding.message
    assert "acme" in finding.message


def test_no_finding_when_autoload_on() -> None:
    assert evaluate_slack_engagement(autoload=True, slack_overlays=["acme"]) is None


def test_no_finding_when_no_slack_overlay_configured() -> None:
    assert evaluate_slack_engagement(autoload=False, slack_overlays=[]) is None


def test_multiple_overlays_are_named_sorted() -> None:
    finding = evaluate_slack_engagement(autoload=False, slack_overlays=["zeta", "acme"])
    assert finding is not None
    assert "acme, zeta" in finding.message


def test_check_renders_the_warn_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_SETTINGS, lambda: _FakeSettings(autoload=False))
    monkeypatch.setattr(_OVERLAYS, lambda: ["acme"])
    lines: list[str] = []

    # Surfacing-only: the WARN is rendered but the return value never gates the exit code.
    assert check_slack_engagement(echo=lines.append) is True
    assert any("WARN" in line and "autoload" in line for line in lines)


def test_check_is_silent_when_autoload_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_SETTINGS, lambda: _FakeSettings(autoload=True))
    monkeypatch.setattr(_OVERLAYS, lambda: ["acme"])
    lines: list[str] = []

    assert check_slack_engagement(echo=lines.append) is True
    assert lines == []


def test_check_never_crashes_on_a_config_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> _FakeSettings:
        raise RuntimeError

    monkeypatch.setattr(_SETTINGS, _boom)
    assert check_slack_engagement(echo=lambda _line: None) is True
