"""``t3 doctor`` Socket Mode readiness — validate + auto-fix per Slack overlay (#106).

Overlays are sourced from the DB ``overlays`` registry (read via the Django ORM
by the doctor, which runs after ``ensure_django``), so the classes seed via
:func:`_seed`.
"""

from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest

from teatree.backends.slack.socket_mode import AppTokenProbe, ManifestSocketGaps
from teatree.cli.slack.manifest import _CONFIG_TOKEN_REF, SlackManifestError, build_manifest
from teatree.cli.slack.socket_doctor import Level, _fixed_message, _no_config_token_message, check_slack_socket_mode
from teatree.core.models import ConfigSetting

_APP_SLOT = "teatree/t3/slack-app"
_BOT_SLOT = "teatree/t3/slack-bot"
_CURRENT_MANIFEST = build_manifest(overlay_name="t3")

_T3_FULL = {
    "messaging_backend": "slack",
    "slack_token_ref": "teatree/t3/slack",
    "slack_app_id": "A_T3",
    "slack_user_id": "U1",
}


def _seed(overlays: dict[str, dict]) -> None:
    ConfigSetting.objects.set_value("overlays", overlays)


def _seed_t3() -> None:
    _seed({"t3": dict(_T3_FULL)})


def _pass(mapping: dict[str, str]) -> Callable[[str], str]:
    return lambda key: mapping.get(key, "")


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestAppTokenValidation:
    def test_missing_app_token_is_actionable(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _BOT_SLOT: "xoxb-b"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=_CURRENT_MANIFEST),
        ):
            outcome = check_slack_socket_mode()
        actions = [f for f in outcome.findings if f.level is Level.ACTION]
        assert any("app-level" in f.message and "connections:write" in f.message for f in actions)
        assert any("pass insert" in f.message and _APP_SLOT in f.message for f in actions)
        # An absent xapp token leaves Socket Mode non-functional until the human
        # mints one — an ACTION counts as not-ok (#3313), though never a FAIL.
        assert outcome.ok is False
        assert not any(f.level is Level.FAIL for f in outcome.findings)

    def test_malformed_app_token_is_fail(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xoxb-wrong-slot"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=_CURRENT_MANIFEST),
        ):
            outcome = check_slack_socket_mode()
        assert any(f.level is Level.FAIL and "xapp-" in f.message for f in outcome.findings)
        assert outcome.ok is False

    def test_app_token_missing_scope_is_reported(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch(
                "teatree.cli.slack.socket_doctor.probe_app_connections",
                return_value=AppTokenProbe(ok=False, missing_scope=True, error="missing_scope"),
            ),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=_CURRENT_MANIFEST),
        ):
            outcome = check_slack_socket_mode()
        assert any(f.level is Level.FAIL and "connections:write" in f.message for f in outcome.findings)

    def test_valid_app_token_reports_ok(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=_CURRENT_MANIFEST),
        ):
            outcome = check_slack_socket_mode()
        assert outcome.ok
        assert any(f.level is Level.OK and "Socket Mode ready" in f.message for f in outcome.findings)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestManifestAutoFix:
    def test_manifest_gap_is_autofixed_via_update(self) -> None:
        _seed_t3()
        stale = build_manifest(overlay_name="t3")
        # Drop reaction_added — the exact gap the auto-fix must close.
        stale["settings"]["event_subscriptions"]["bot_events"] = ["app_mention", "message.im"]
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a", _BOT_SLOT: "xoxb-b"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=stale),
            patch("teatree.cli.slack.socket_doctor.update_manifest", return_value={"permissions_updated": True}) as upd,
        ):
            outcome = check_slack_socket_mode()
        upd.assert_called_once()
        # The manifest was rewritten, but Socket Mode is live only after the
        # operator reinstalls — an ACTION (awaiting reinstall), not OK (#3313).
        assert any(f.level is Level.ACTION and "reaction_added" in f.message for f in outcome.findings)
        assert any("Reinstall" in f.message for f in outcome.findings)
        assert outcome.ok is False

    def test_no_config_token_degrades_manifest_to_action(self) -> None:
        _seed_t3()
        stale = build_manifest(overlay_name="t3")
        stale["settings"]["socket_mode_enabled"] = False
        mapping = {_APP_SLOT: "xapp-a"}  # no app-config token
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=stale),
            patch("teatree.cli.slack.socket_doctor.update_manifest") as upd,
        ):
            outcome = check_slack_socket_mode()
        upd.assert_not_called()
        assert any(f.level is Level.ACTION and _CONFIG_TOKEN_REF in f.message for f in outcome.findings)

    def test_current_manifest_reports_ok(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=_CURRENT_MANIFEST),
            patch("teatree.cli.slack.socket_doctor.update_manifest") as upd,
        ):
            outcome = check_slack_socket_mode()
        upd.assert_not_called()
        assert any(f.level is Level.OK and "current" in f.message for f in outcome.findings)

    def test_dm_only_manifest_is_not_rewidened(self) -> None:
        # The footgun guard: a dm_only overlay whose live manifest is already the
        # narrow dm_only set must report OK — the doctor must NOT push the full
        # manifest and re-grant the channel scopes the operator dropped.
        _seed({"t3": {**_T3_FULL, "slack_scope_profile": "dm_only"}})
        dm_current = build_manifest(overlay_name="t3", scope_profile="dm_only")
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=dm_current),
            patch("teatree.cli.slack.socket_doctor.update_manifest") as upd,
        ):
            outcome = check_slack_socket_mode()
        upd.assert_not_called()
        assert any(f.level is Level.OK and "current" in f.message for f in outcome.findings)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestOverlaySelection:
    def test_no_slack_overlays_yields_no_findings(self) -> None:
        _seed({"t3": {"path": "/repo"}})
        assert check_slack_socket_mode().findings == ()

    def test_one_overlay_failure_does_not_abort_others(self) -> None:
        _seed(
            {
                "t3": {"messaging_backend": "slack", "slack_token_ref": "teatree/t3/slack", "slack_app_id": "A_T3"},
                "two": {"messaging_backend": "slack", "slack_token_ref": "teatree/two/slack", "slack_app_id": "A_TWO"},
            }
        )
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a", "teatree/two/slack-app": "xapp-b"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch(
                "teatree.cli.slack.socket_doctor._export_with_rotation",
                side_effect=[RuntimeError("boom"), build_manifest(overlay_name="two")],
            ),
        ):
            outcome = check_slack_socket_mode()
        overlays_seen = {f.overlay for f in outcome.findings}
        assert overlays_seen == {"t3", "two"}
        assert any(f.overlay == "t3" and f.level is Level.WARN and "crashed" in f.message for f in outcome.findings)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDegradedPaths:
    def _seed_missing_fields(self) -> None:
        _seed({"t3": {"messaging_backend": "slack"}})

    def test_no_token_ref_and_no_app_id_warn(self) -> None:
        self._seed_missing_fields()
        with patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass({})):
            outcome = check_slack_socket_mode()
        assert any(f.level is Level.WARN and "slack_token_ref" in f.message for f in outcome.findings)
        assert any(f.level is Level.WARN and "slack_app_id" in f.message for f in outcome.findings)
        assert outcome.ok

    def test_probe_network_error_warns(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", side_effect=httpx.ConnectError("down")),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=_CURRENT_MANIFEST),
        ):
            outcome = check_slack_socket_mode()
        assert any(f.level is Level.WARN and "could not reach Slack" in f.message for f in outcome.findings)

    def test_probe_other_error_warns(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch(
                "teatree.cli.slack.socket_doctor.probe_app_connections",
                return_value=AppTokenProbe(ok=False, missing_scope=False, error="invalid_auth"),
            ),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=_CURRENT_MANIFEST),
        ):
            outcome = check_slack_socket_mode()
        assert any(f.level is Level.WARN and "invalid_auth" in f.message for f in outcome.findings)

    def test_manifest_export_error_warns(self) -> None:
        _seed_t3()
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch(
                "teatree.cli.slack.socket_doctor._export_with_rotation",
                side_effect=SlackManifestError("invalid_auth"),
            ),
        ):
            outcome = check_slack_socket_mode()
        assert any(f.level is Level.WARN and "could not export the manifest" in f.message for f in outcome.findings)

    def test_manifest_update_error_warns(self) -> None:
        _seed_t3()
        stale = build_manifest(overlay_name="t3")
        stale["settings"]["event_subscriptions"]["bot_events"] = ["app_mention", "message.im"]
        mapping = {_CONFIG_TOKEN_REF: "cfg", _APP_SLOT: "xapp-a"}
        with (
            patch("teatree.cli.slack.socket_doctor.read_pass", side_effect=_pass(mapping)),
            patch("teatree.cli.slack.socket_doctor.probe_app_connections", return_value=AppTokenProbe.valid()),
            patch("teatree.cli.slack.socket_doctor._export_with_rotation", return_value=stale),
            patch("teatree.cli.slack.socket_doctor.update_manifest", side_effect=SlackManifestError("boom")),
        ):
            outcome = check_slack_socket_mode()
        assert any(f.level is Level.WARN and "manifest update failed" in f.message for f in outcome.findings)


class TestFindingMessages:
    def test_fixed_message_lists_every_gap_kind(self) -> None:
        gaps = ManifestSocketGaps(
            socket_mode_disabled=True,
            missing_events=frozenset({"reaction_added"}),
            missing_bot_scopes=frozenset({"im:history"}),
        )
        message = _fixed_message(gaps, "A1")
        assert "enabled Socket Mode" in message
        assert "reaction_added" in message
        assert "im:history" in message
        assert "install-on-team" in message

    def test_no_config_token_message_lists_every_gap_kind(self) -> None:
        gaps = ManifestSocketGaps(
            socket_mode_disabled=True,
            missing_events=frozenset({"app_mention"}),
            missing_bot_scopes=frozenset({"reactions:read"}),
        )
        message = _no_config_token_message("A1", gaps)
        assert "Socket Mode disabled" in message
        assert "app_mention" in message
        assert "reactions:read" in message
        assert _CONFIG_TOKEN_REF in message
