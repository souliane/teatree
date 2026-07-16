"""Tests for ``t3 setup slack-bot`` — interactive Slack-bot walkthrough.

Overlay Slack settings are DB-home: reads/writes go through ``ConfigSetting`` and
the single ``overlays`` registry row (``{name: {fields}}``). Config-touching
classes are DB-backed and seed via :func:`_seed_overlays` / assert via
:func:`_overlays`.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.setup import setup_app
from teatree.cli.slack.setup import (
    _APP_ID_RE,
    _BOT_ONLY_SCOPES,
    _BOT_TOKEN_RE,
    _USER_ID_RE,
    _USER_SCOPES,
    SlackManifestError,
    _user_scopes_carry_no_bot_only_scope,
    app_install_url,
    app_manifest_editor_url,
    build_manifest,
    export_manifest,
    manifest_install_url,
    manifests_equivalent,
    rotate_config_token,
    update_manifest,
    write_overlay_settings,
)
from teatree.config import OverlayEntry
from teatree.core.models import ConfigSetting


def _seed_overlays(overlays: dict[str, dict]) -> None:
    ConfigSetting.objects.set_value("overlays", overlays)


def _overlays() -> dict:
    return ConfigSetting.objects.get_effective("overlays") or {}


def _seed_cold_registry(db: Path, overlays: dict[str, dict]) -> None:
    """Seed a cold-readable config DB — the tier ``discover_overlays`` reads (pre-Django)."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps(overlays),),
        )
        conn.commit()
    finally:
        conn.close()


class TestBuildManifest:
    def test_default_display_name_uses_overlay(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        assert manifest["display_information"]["name"] == "teatree-acme"

    def test_socket_mode_enabled_no_interactivity(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        assert manifest["settings"]["socket_mode_enabled"] is True
        assert manifest["settings"]["interactivity"]["is_enabled"] is False

    def test_required_bot_scopes_present(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        scopes = manifest["oauth_config"]["scopes"]["bot"]
        for required in ("chat:write", "im:write", "im:history", "reactions:read", "reactions:write"):
            assert required in scopes

    def test_bot_scopes_grant_files_write_for_speak_audio(self) -> None:
        """The bot token must hold ``files:write`` for the ``[teatree.speak] slack`` attach.

        ``SlackBotBackend.post_audio_dm`` attaches the synthesised ``.m4a``
        to the user's own DM, which ``_route_token`` sends under the **bot**
        token. Without ``files:write`` in the manifest a reinstall never
        grants the scope, ``files.getUploadURLExternal`` returns
        ``missing_scope``, and ``slack = true`` can never attach the
        audio — the exact gap the speak docstrings tell the user a
        ``t3 setup slack-bot`` reinstall would close.
        """
        manifest = build_manifest(overlay_name="acme")
        assert "files:write" in manifest["oauth_config"]["scopes"]["bot"]

    def test_subscribed_to_app_mention_and_dm(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        events = manifest["settings"]["event_subscriptions"]["bot_events"]
        assert "app_mention" in events
        assert "message.im" in events

    def test_user_token_scopes_grant_reactions(self) -> None:
        """The manifest must request ``user`` (xoxp) scopes, not only ``bot``.

        The xoxp user token is the only credential that can post reactions in
        Slack-Connect externally-shared channels (the bot token is rejected
        with ``mcp_externally_shared_channel_restricted``). When the manifest
        declares no ``user`` scopes section, a reinstall never re-prompts for
        ``reactions:write`` consent and ``SlackBotBackend.react`` /
        ``get_reactions`` (which route through the user token) silently fail.
        """
        manifest = build_manifest(overlay_name="acme")
        user_scopes = manifest["oauth_config"]["scopes"]["user"]
        assert "reactions:write" in user_scopes
        assert "reactions:read" in user_scopes

    def test_user_scopes_keep_existing_capability_on_reinstall(self) -> None:
        """A reinstall re-consents to exactly the manifest ``user`` set.

        Slack drops any user scope not listed, so the set must be a superset
        that preserves the capability the xoxp token is already relied on:
        ``chat:write`` (posting in Slack-Connect channels under the user's
        identity) and ``users:read`` (handle/id resolution). Listing only the
        two reaction scopes would silently revoke those on reinstall.
        """
        manifest = build_manifest(overlay_name="acme")
        user_scopes = manifest["oauth_config"]["scopes"]["user"]
        for required in ("reactions:read", "reactions:write", "chat:write", "users:read"):
            assert required in user_scopes

    def test_bot_scopes_still_present_alongside_user_scopes(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        scopes = manifest["oauth_config"]["scopes"]
        assert "bot" in scopes
        assert "user" in scopes
        assert "chat:write" in scopes["bot"]


class TestDmOnlyManifest:
    """The ``dm_only`` profile narrows the app to its owner's 1:1 DM."""

    def test_no_user_scope_section(self) -> None:
        # A DM-only bot needs no xoxp token, so the manifest must omit the whole
        # user section — a reinstall then revokes any user scope the app held.
        manifest = build_manifest(overlay_name="t3-teatree", scope_profile="dm_only")
        assert "user" not in manifest["oauth_config"]["scopes"]

    def test_bot_scopes_are_dm_minimal(self) -> None:
        scopes = manifest_bot_scopes("dm_only")
        # Present: everything needed to post/read/react in the owner DM.
        for required in ("chat:write", "im:write", "im:history", "im:read", "reactions:write", "users:read"):
            assert required in scopes
        # Absent: no channel/group/mpim reach, no channel mentions.
        for forbidden in ("channels:history", "channels:read", "groups:read", "mpim:read", "app_mentions:read"):
            assert forbidden not in scopes

    def test_events_drop_app_mention(self) -> None:
        events = build_manifest(overlay_name="t3-teatree", scope_profile="dm_only")
        bot_events = events["settings"]["event_subscriptions"]["bot_events"]
        assert "app_mention" not in bot_events
        assert "message.im" in bot_events

    def test_full_profile_still_has_channel_scopes(self) -> None:
        # Regression guard: narrowing dm_only must not narrow the default.
        scopes = manifest_bot_scopes("full")
        assert "channels:history" in scopes
        assert "user" in build_manifest(overlay_name="acme")["oauth_config"]["scopes"]

    def test_unknown_profile_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="scope_profile"):
            build_manifest(overlay_name="acme", scope_profile="bogus")


def manifest_bot_scopes(profile: str) -> list[str]:
    return build_manifest(overlay_name="x", scope_profile=profile)["oauth_config"]["scopes"]["bot"]


class TestUserScopesExcludeBotOnly:
    """``_USER_SCOPES`` must list only scopes Slack grants on a *user* token.

    A user token (``xoxp-…``) minted via ``apps.manifest.update`` is rejected
    with ``illegal_user_scopes`` if the manifest's ``oauth_config.scopes.user``
    contains a bot-only scope. The data-driven ``_BOT_ONLY_SCOPES`` set is the
    guard: any future bot-only scope added to it is automatically enforced
    against both the manifest and the verifier without touching this test.
    """

    def test_known_bot_only_scopes_enumerated(self) -> None:
        assert {"chat:write.customize", "chat:write.public"} <= _BOT_ONLY_SCOPES

    def test_user_scopes_contain_no_bot_only_scope(self) -> None:
        assert _BOT_ONLY_SCOPES.isdisjoint(_USER_SCOPES)

    def test_built_manifest_user_section_has_no_bot_only_scope(self) -> None:
        user_scopes = build_manifest(overlay_name="acme")["oauth_config"]["scopes"]["user"]
        assert _BOT_ONLY_SCOPES.isdisjoint(user_scopes)

    def test_guard_raises_when_a_bot_only_scope_leaks_in(self) -> None:
        leaked = min(_BOT_ONLY_SCOPES)
        with (
            patch("teatree.cli.slack.manifest._USER_SCOPES", [*_USER_SCOPES, leaked]),
            pytest.raises(AssertionError, match=re.escape(leaked)),
        ):
            _user_scopes_carry_no_bot_only_scope()


class TestManifestInstallUrl:
    def test_url_targets_create_app_endpoint(self) -> None:
        url = manifest_install_url(build_manifest(overlay_name="acme"))
        assert url.startswith("https://api.slack.com/apps?new_app=1&manifest_json=")

    def test_url_carries_overlay_name_in_payload(self) -> None:
        url = manifest_install_url(build_manifest(overlay_name="acme"))
        assert "teatree-acme" in url


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestWriteOverlaySettings:
    def test_creates_overlays_entry_when_missing(self) -> None:
        write_overlay_settings(
            "acme",
            slack_user_id="U01ABCD1234",
            slack_token_ref="teatree/acme/slack",
        )
        entry = _overlays()["acme"]
        assert entry["slack_user_id"] == "U01ABCD1234"
        assert entry["slack_token_ref"] == "teatree/acme/slack"
        assert entry["messaging_backend"] == "slack"

    def test_preserves_unrelated_keys(self) -> None:
        _seed_overlays(
            {
                "acme": {"path": "/p/acme"},
                "beta": {"path": "/p/beta", "slack_user_id": "U999"},
            }
        )
        write_overlay_settings(
            "acme",
            slack_user_id="U01ABCD1234",
            slack_token_ref="teatree/acme/slack",
        )
        registry = _overlays()
        assert registry["acme"]["path"] == "/p/acme"
        assert registry["acme"]["slack_user_id"] == "U01ABCD1234"
        assert registry["beta"]["slack_user_id"] == "U999"
        assert registry["beta"]["path"] == "/p/beta"

    def test_overwrites_existing_slack_settings(self) -> None:
        _seed_overlays({"acme": {"slack_user_id": "U_OLD", "slack_token_ref": "old-ref"}})
        write_overlay_settings(
            "acme",
            slack_user_id="U_NEW",
            slack_token_ref="new-ref",
        )
        entry = _overlays()["acme"]
        assert entry["slack_user_id"] == "U_NEW"
        assert entry["slack_token_ref"] == "new-ref"


class TestPatterns:
    @pytest.mark.parametrize(
        "value",
        ["xoxb-1234567-abc-DEF", "xoxb-0-z"],
    )
    def test_bot_token_accepts_valid(self, value: str) -> None:
        assert _BOT_TOKEN_RE.match(value)

    @pytest.mark.parametrize(
        "value",
        ["xapp-1-A1-BC", "abc", "xoxp-1234", " xoxb-1"],
    )
    def test_bot_token_rejects_invalid(self, value: str) -> None:
        assert _BOT_TOKEN_RE.match(value) is None

    @pytest.mark.parametrize(
        "value",
        ["U01ABCD1234", "W0012345"],
    )
    def test_user_id_accepts_valid(self, value: str) -> None:
        assert _USER_ID_RE.match(value)

    @pytest.mark.parametrize(
        "value",
        ["alice", "u01abcd", "U-1234567"],
    )
    def test_user_id_rejects_invalid(self, value: str) -> None:
        assert _USER_ID_RE.match(value) is None


def _stub_overlays() -> list[OverlayEntry]:
    return [OverlayEntry(name="acme", overlay_class="acme.overlay:AcmeOverlay")]


def _invoke_setup(*, inputs: str, args: list[str]) -> object:
    runner = CliRunner()
    return runner.invoke(
        setup_app,
        ["slack-bot", *args],
        input=inputs,
        catch_exceptions=False,
    )


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestSlackBotCommand:
    def test_unknown_overlay_exits_with_error(self) -> None:
        with patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()):
            result = _invoke_setup(inputs="", args=["--overlay", "ghost"])
        assert result.exit_code == 1
        assert "not registered" in result.stdout

    def test_skip_smoke_test_writes_tokens_and_settings(self) -> None:
        captured: dict[str, str] = {}

        def fake_write_pass(key: str, value: str) -> bool:
            captured[key] = value
            return True

        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", side_effect=fake_write_pass),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert captured["teatree/acme/slack-bot"] == "xoxb-1-test"
        assert captured["teatree/acme/slack-app"] == "xapp-1-test"
        entry = _overlays()["acme"]
        assert entry["slack_user_id"] == "U01ABCD1234"
        assert entry["slack_token_ref"] == "teatree/acme/slack"

    def test_reset_skips_manifest_url(self) -> None:
        opened: list[str] = []
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open", side_effect=opened.append),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--reset", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert opened == []
        assert "Reset mode" in result.stdout

    def test_reset_warns_scope_change_needs_full_reinstall(self) -> None:
        """``--reset`` must tell the user a scope change is NOT applied by reset.

        Adding ``reactions:write`` to the xoxp user token only takes effect
        through a full (non-``--reset``) manifest reinstall with browser OAuth
        re-consent; ``--reset`` merely rotates the existing tokens. The command
        must say so or the user keeps reinstalling via ``--reset`` and never
        gets the new scope (the root cause this fix addresses).
        """
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--reset", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert "scope change" in result.stdout
        assert "without --reset" in result.stdout

    def test_full_install_prints_user_token_scope_guidance(self) -> None:
        """A non-``--reset`` run instructs the user to approve User Token Scopes."""
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert "User Token Scopes" in result.stdout
        assert "reactions:write" in result.stdout

    def test_pass_failure_exits_with_error(self) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=False),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 1
        assert "`pass insert teatree/acme/slack-bot` failed" in result.stdout

    def test_app_token_pass_failure_exits_with_error(self) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"

        def fake_write_pass(key: str, value: str) -> bool:
            return key.endswith("-bot")  # bot succeeds, app fails

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", side_effect=fake_write_pass),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 1
        assert "`pass insert teatree/acme/slack-app` failed" in result.stdout

    def test_invalid_token_format_reprompts(self) -> None:
        inputs = "garbage\nxoxb-1-test\nxapp-1-test\nU01ABCD1234\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert "Invalid bot token format" in result.stdout

    def test_invalid_user_id_format_reprompts(self) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nbad-id\nU01ABCD1234\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert "Slack user ids start with" in result.stdout


class TestSmokeTest:
    """Direct tests for ``_smoke_test`` — bypasses the CLI prompts."""

    def test_open_dm_returns_empty_channel(self) -> None:
        from teatree.cli.slack.setup import _smoke_test  # noqa: PLC0415 — scoped import inside the test method

        with patch("teatree.cli.slack.setup.SlackBotBackend") as bot_cls:
            bot_cls.return_value.open_dm.return_value = ""
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is False

    def test_post_message_failure(self) -> None:
        from teatree.cli.slack.setup import _smoke_test  # noqa: PLC0415 — scoped import inside the test method

        with patch("teatree.cli.slack.setup.SlackBotBackend") as bot_cls:
            bot_cls.return_value.open_dm.return_value = "C123"
            bot_cls.return_value.post_message.return_value = {"error": "channel_not_found"}
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is False

    def test_reaction_received_within_timeout(self) -> None:
        from teatree.cli.slack.setup import _smoke_test  # noqa: PLC0415 — scoped import inside the test method

        with (
            patch("teatree.cli.slack.setup.SlackBotBackend") as bot_cls,
            patch("teatree.cli.slack.setup.time.sleep"),
        ):
            bot_cls.return_value.open_dm.return_value = "C123"
            bot_cls.return_value.post_message.return_value = {"ts": "1.0"}
            bot_cls.return_value.get_reactions.return_value = ["white_check_mark"]
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is True

    def test_reaction_timeout_returns_false(self) -> None:
        from teatree.cli.slack.setup import _smoke_test  # noqa: PLC0415 — scoped import inside the test method

        # First monotonic call returns 0 (start), subsequent calls return values
        # past the deadline so the while loop exits without polling.
        with (
            patch("teatree.cli.slack.setup.SlackBotBackend") as bot_cls,
            patch("teatree.cli.slack.setup.time.sleep"),
            patch("teatree.cli.slack.setup.time.monotonic", side_effect=[0.0, 1.0, 9_999_999.0]),
        ):
            bot_cls.return_value.open_dm.return_value = "C123"
            bot_cls.return_value.post_message.return_value = {"ts": "1.0"}
            bot_cls.return_value.get_reactions.return_value = []
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is False


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestSmokeTestInvocation:
    def test_smoke_test_failure_exits_with_error(self) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup._smoke_test", return_value=False),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme"])

        assert result.exit_code == 1


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestFinishSelfHealsUserId:
    """The update path (app id recorded) skips the "record your user id" prompt.

    Without self-healing, ``slack_user_id`` stays ``""`` and the runtime
    ``SlackBotBackend`` can neither DM nor read the owner. ``_finish_with_smoke_test``
    resolves the empty id from the canonical ``pass slack/user-id`` entry.
    """

    def test_empty_registry_user_id_is_healed_from_pass(self) -> None:
        from teatree.cli.slack.app_resolve import read_overlay_field  # noqa: PLC0415 — scoped import
        from teatree.cli.slack.setup import _finish_with_smoke_test  # noqa: PLC0415 — scoped import

        _seed_overlays({"acme": {"slack_app_id": "A123456", "slack_user_id": ""}})
        with patch("teatree.cli.slack.setup.read_pass", return_value="U0OWNER1234"):
            _finish_with_smoke_test(
                overlay="acme",
                app_id="A123456",
                token_ref="teatree/acme/slack",
                skip_smoke_test=True,
            )
        assert read_overlay_field("acme", "slack_user_id") == "U0OWNER1234"

    def test_empty_pass_leaves_user_id_empty(self) -> None:
        # No canonical pass id — self-heal must NOT invent a bogus (bot) id.
        from teatree.cli.slack.app_resolve import read_overlay_field  # noqa: PLC0415 — scoped import
        from teatree.cli.slack.setup import _finish_with_smoke_test  # noqa: PLC0415 — scoped import

        _seed_overlays({"acme": {"slack_app_id": "A123456", "slack_user_id": ""}})
        with patch("teatree.cli.slack.setup.read_pass", return_value=""):
            _finish_with_smoke_test(
                overlay="acme",
                app_id="A123456",
                token_ref="teatree/acme/slack",
                skip_smoke_test=True,
            )
        assert read_overlay_field("acme", "slack_user_id") == ""

    def test_recorded_user_id_is_not_overwritten_by_pass(self) -> None:
        from teatree.cli.slack.app_resolve import read_overlay_field  # noqa: PLC0415 — scoped import
        from teatree.cli.slack.setup import _finish_with_smoke_test  # noqa: PLC0415 — scoped import

        _seed_overlays({"acme": {"slack_app_id": "A123456", "slack_user_id": "U_RECORDED"}})
        with patch("teatree.cli.slack.setup.read_pass", return_value="U0OWNER1234") as read:
            _finish_with_smoke_test(
                overlay="acme",
                app_id="A123456",
                token_ref="teatree/acme/slack",
                skip_smoke_test=True,
            )
        assert read_overlay_field("acme", "slack_user_id") == "U_RECORDED"
        read.assert_not_called()


class TestManifestsEquivalent:
    """Normalised compare of only the teatree-owned manifest fields."""

    def test_identical_manifests_are_equivalent(self) -> None:
        a = build_manifest(overlay_name="acme")
        b = build_manifest(overlay_name="acme")
        assert manifests_equivalent(a, b) is True

    def test_scope_reordering_same_set_is_equivalent(self) -> None:
        a = build_manifest(overlay_name="acme")
        b = build_manifest(overlay_name="acme")
        b["oauth_config"]["scopes"]["bot"] = list(reversed(b["oauth_config"]["scopes"]["bot"]))
        b["oauth_config"]["scopes"]["user"] = list(reversed(b["oauth_config"]["scopes"]["user"]))
        b["settings"]["event_subscriptions"]["bot_events"] = list(
            reversed(b["settings"]["event_subscriptions"]["bot_events"])
        )
        assert manifests_equivalent(a, b) is True

    def test_added_user_scope_is_not_equivalent(self) -> None:
        a = build_manifest(overlay_name="acme")
        b = build_manifest(overlay_name="acme")
        b["oauth_config"]["scopes"]["user"] = [*b["oauth_config"]["scopes"]["user"], "channels:read"]
        assert manifests_equivalent(a, b) is False

    def test_changed_display_name_is_not_equivalent(self) -> None:
        a = build_manifest(overlay_name="acme")
        b = build_manifest(overlay_name="acme", display_name="teatree-renamed")
        assert manifests_equivalent(a, b) is False


class TestExportUpdateRotate:
    def test_export_returns_manifest(self) -> None:
        with patch(
            "teatree.cli.slack.manifest._slack_app_api",
            return_value={"ok": True, "manifest": {"display_information": {"name": "x"}}},
        ):
            manifest = export_manifest(app_id="A123456", config_token="xoxe.xoxp-1")
        assert manifest == {"display_information": {"name": "x"}}

    def test_export_not_ok_raises_manifest_error(self) -> None:
        with (
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": False, "error": "invalid_auth"},
            ),
            pytest.raises(SlackManifestError, match="invalid_auth"),
        ):
            export_manifest(app_id="A123456", config_token="xoxe.xoxp-1")

    def test_update_posts_app_id_and_json(self) -> None:
        captured: dict[str, Any] = {}

        def fake_api(method: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
            captured["method"] = method
            captured["payload"] = payload
            captured["token"] = token
            return {"ok": True, "permissions_updated": True}

        with patch("teatree.cli.slack.manifest._slack_app_api", side_effect=fake_api):
            result = update_manifest(
                app_id="A123456",
                manifest={"display_information": {"name": "x"}},
                config_token="xoxe.xoxp-1",
            )
        assert captured["method"] == "apps.manifest.update"
        assert captured["payload"]["app_id"] == "A123456"
        assert json.loads(captured["payload"]["manifest"]) == {"display_information": {"name": "x"}}
        assert result["permissions_updated"] is True

    def test_update_not_ok_raises_manifest_error(self) -> None:
        with (
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": False, "error": "failed_constraint"},
            ),
            pytest.raises(SlackManifestError, match="failed_constraint"),
        ):
            update_manifest(app_id="A1", manifest={}, config_token="xoxe.xoxp-1")

    def test_rotate_returns_access_and_refresh(self) -> None:
        with patch(
            "teatree.cli.slack.manifest._slack_app_api",
            return_value={"ok": True, "token": "xoxe.xoxp-NEW", "refresh_token": "xoxe-NEWREFRESH"},
        ):
            access, refresh = rotate_config_token(refresh_token="xoxe-OLD")
        assert access == "xoxe.xoxp-NEW"
        assert refresh == "xoxe-NEWREFRESH"

    def test_rotate_not_ok_raises_manifest_error(self) -> None:
        with (
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": False, "error": "token_expired"},
            ),
            pytest.raises(SlackManifestError, match="token_expired"),
        ):
            rotate_config_token(refresh_token="xoxe-OLD")


class TestDeepLinks:
    def test_app_manifest_editor_url(self) -> None:
        assert app_manifest_editor_url("A123456") == "https://api.slack.com/apps/A123456/app-manifest"

    def test_app_install_url(self) -> None:
        assert app_install_url("A123456") == "https://api.slack.com/apps/A123456/install-on-team"


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestWriteOverlaySettingsAppId:
    def test_app_id_written_when_provided(self) -> None:
        write_overlay_settings(
            "acme",
            slack_user_id="U01ABCD1234",
            slack_token_ref="teatree/acme/slack",
            slack_app_id="A123456",
        )
        assert _overlays()["acme"]["slack_app_id"] == "A123456"

    def test_app_id_absent_when_empty(self) -> None:
        write_overlay_settings(
            "acme",
            slack_user_id="U01ABCD1234",
            slack_token_ref="teatree/acme/slack",
        )
        assert "slack_app_id" not in _overlays()["acme"]

    def test_unrelated_keys_preserved(self) -> None:
        _seed_overlays({"acme": {"path": "/p/acme"}})
        write_overlay_settings(
            "acme",
            slack_user_id="U01ABCD1234",
            slack_token_ref="teatree/acme/slack",
            slack_app_id="A123456",
        )
        entry = _overlays()["acme"]
        assert entry["path"] == "/p/acme"
        assert entry["slack_app_id"] == "A123456"


class TestAppIdPattern:
    @pytest.mark.parametrize("value", ["A123456", "A0ABCD1234XYZ"])
    def test_accepts_valid(self, value: str) -> None:
        assert _APP_ID_RE.match(value)

    @pytest.mark.parametrize("value", ["B123456", "A12345", "a123456", "A-123456"])
    def test_rejects_invalid(self, value: str) -> None:
        assert _APP_ID_RE.match(value) is None


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestUpdatePathModeResolution:
    def test_recorded_app_id_takes_update_path(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A123456"}})
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", return_value="xoxe.xoxp-token"),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": True, "manifest": build_manifest(overlay_name="acme")},
            ),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 0, result.stdout
        assert "Create New App" not in result.stdout

    def test_update_flag_no_id_prompts_and_validates_app_id(self) -> None:
        # bad app id first, then a valid one (reprompt path).
        inputs = "bad-id\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", return_value="xoxe.xoxp-token"),
            patch("teatree.cli.slack.app_resolve.read_pass", return_value=""),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": True, "manifest": build_manifest(overlay_name="acme")},
            ),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--update"])

        assert result.exit_code == 0, result.stdout
        assert "Slack app id" in result.stdout

    def test_no_id_no_flags_takes_create_path_and_records_id(self) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert "Create New App" in result.stdout
        assert _overlays()["acme"]["slack_app_id"] == "A123456"

    def test_reset_still_rotate_only(self) -> None:
        opened: list[str] = []
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open", side_effect=opened.append),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme", "--reset", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert opened == []
        assert "Reset mode" in result.stdout


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestUpdatePathBehavior:
    def _seed_app(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A123456"}})

    def test_manifest_unchanged_is_noop(self) -> None:
        self._seed_app()
        current = build_manifest(overlay_name="acme")
        calls: list[str] = []

        def fake_api(method: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
            calls.append(method)
            return {"ok": True, "manifest": current}

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", return_value="xoxe.xoxp-token"),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True) as smoke,
            patch("teatree.cli.slack.manifest._slack_app_api", side_effect=fake_api),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 0, result.stdout
        assert "apps.manifest.update" not in calls
        assert "already current" in result.stdout
        smoke.assert_called_once()

    def test_manifest_changed_updates_and_prints_install_link(self) -> None:
        self._seed_app()
        stale = build_manifest(overlay_name="acme")
        stale["oauth_config"]["scopes"]["user"] = ["users:read"]
        opened: list[str] = []

        def fake_api(method: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
            if method == "apps.manifest.export":
                return {"ok": True, "manifest": stale}
            return {"ok": True, "permissions_updated": True}

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open", side_effect=opened.append),
            patch("teatree.cli.slack.setup.read_pass", return_value="xoxe.xoxp-token"),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
            patch("teatree.cli.slack.manifest._slack_app_api", side_effect=fake_api),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 0, result.stdout
        assert "Manifest updated" in result.stdout
        assert "ACTION" in result.stdout
        assert "https://api.slack.com/apps/A123456/install-on-team" in opened

    def test_config_token_expired_with_refresh_rotates_and_retries(self) -> None:
        self._seed_app()
        current = build_manifest(overlay_name="acme")
        seq = iter(
            [
                {"ok": False, "error": "token_expired"},  # first export
                {"ok": True, "token": "xoxe.xoxp-NEW", "refresh_token": "xoxe-NEWR"},  # rotate
                {"ok": True, "manifest": current},  # retry export
            ]
        )
        written: dict[str, str] = {}

        def fake_api(method: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
            return next(seq)

        def fake_read(key: str) -> str:
            if key == "teatree/slack-app-config-token":
                return "xoxe.xoxp-OLD"
            if key == "teatree/slack-app-config-refresh":
                return "xoxe-OLDR"
            return "xoxb-bot"

        def fake_write(key: str, value: str) -> bool:
            written[key] = value
            return True

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.setup.write_pass", side_effect=fake_write),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
            patch("teatree.cli.slack.manifest._slack_app_api", side_effect=fake_api),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 0, result.stdout
        assert written["teatree/slack-app-config-token"] == "xoxe.xoxp-NEW"
        assert written["teatree/slack-app-config-refresh"] == "xoxe-NEWR"

    def test_config_token_expired_no_refresh_is_degraded_nonzero(self) -> None:
        self._seed_app()

        def fake_read(key: str) -> str:
            if key == "teatree/slack-app-config-token":
                return "xoxe.xoxp-OLD"
            return ""  # no refresh, no bot

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": False, "error": "token_expired"},
            ),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 1
        assert "config token expired" in result.stdout


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDegradedPath:
    def test_no_config_token_prints_editor_url_and_smoke_tests(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A123456"}})
        opened: list[str] = []

        def fake_read(key: str) -> str:
            if key == "teatree/slack-app-config-token":
                return ""  # no config token -> degraded
            return "xoxb-bot"

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open", side_effect=opened.append),
            patch("teatree.cli.slack.setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True) as smoke,
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 0, result.stdout
        assert "https://api.slack.com/apps/A123456/app-manifest" in opened
        assert "new_app=1" not in result.stdout
        assert '"display_information"' in result.stdout
        smoke.assert_called_once()


class TestSlackAppApi:
    """``_slack_app_api`` is the single Slack HTTP boundary."""

    def test_posts_with_bearer_token_and_returns_json(self) -> None:
        from teatree.cli.slack.setup import _slack_app_api  # noqa: PLC0415 — scoped import inside the test method

        captured: dict[str, Any] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                captured["raised"] = True

            def json(self) -> dict[str, Any]:
                return {"ok": True, "manifest": {}}

        def fake_post(url: str, *, headers: dict[str, str], data: dict[str, Any], timeout: int) -> FakeResponse:
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            return FakeResponse()

        with patch("teatree.cli.slack.manifest.httpx.post", side_effect=fake_post):
            result = _slack_app_api("apps.manifest.export", {"app_id": "A1"}, token="xoxe.xoxp-1")

        assert captured["url"] == "https://slack.com/api/apps.manifest.export"
        assert captured["headers"]["Authorization"] == "Bearer xoxe.xoxp-1"
        assert captured["data"] == {"app_id": "A1"}
        assert captured["raised"] is True
        assert result == {"ok": True, "manifest": {}}


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestUpdatePathSmokeFailure:
    def test_update_path_smoke_failure_exits_nonzero(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A123456"}})
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", return_value="xoxe.xoxp-token"),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=False),
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": True, "manifest": build_manifest(overlay_name="acme")},
            ),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 1

    def test_degraded_path_skip_smoke_test_returns_zero(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A123456"}})
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", return_value=""),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test") as smoke,
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        smoke.assert_not_called()

    def test_create_path_smoke_success_returns_zero(self) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\nA123456\n"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
        ):
            result = _invoke_setup(inputs=inputs, args=["--overlay", "acme"])

        assert result.exit_code == 0, result.stdout

    def test_unexpected_manifest_error_exits_nonzero(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A123456"}})
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup.read_pass", return_value="xoxe.xoxp-token"),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
            patch(
                "teatree.cli.slack.manifest._slack_app_api",
                return_value={"ok": False, "error": "ratelimited"},
            ),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme"])

        assert result.exit_code == 1
        assert "Slack manifest API failed" in result.stdout


class TestValidateOverlayKnownList:
    """``_validate_overlay`` must not list bare ``teatree`` (souliane/teatree#1108).

    The "Known overlays: ..." line is the user-visible symptom from the ticket.
    With a legacy bare ``teatree`` entry in the DB overlays registry (written by
    older ``slack-bot`` runs) ``discover_overlays`` used to emit both ``teatree``
    and ``t3-teatree``; the error message then offered the bogus ``teatree`` as a
    selectable overlay. The bundled overlay's only canonical name is the
    entry-point name ``t3-teatree``. ``discover_overlays`` reads the registry via
    the pre-Django ``cold_reader``, so the seed goes in a cold-readable DB.
    """

    def test_validate_overlay_does_not_list_bare_teatree(self, tmp_path: Path, monkeypatch) -> None:
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        import typer  # noqa: PLC0415

        from teatree.cli.slack.setup import _validate_overlay  # noqa: PLC0415 — scoped import inside the test method

        db = tmp_path / "config.sqlite3"
        _seed_cold_registry(db, {"teatree": {"mode": "auto"}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        real_ep = MagicMock()
        real_ep.name = "t3-teatree"
        real_ep.value = "teatree.contrib.t3_teatree.overlay:TeatreeOverlay"

        out = io.StringIO()
        with (
            patch("importlib.metadata.entry_points", return_value=[real_ep]),
            patch("teatree.config.discovery._resolve_ep_project_path", return_value=None),
            pytest.raises(typer.Exit),
            redirect_stdout(out),
        ):
            _validate_overlay("ghost")

        message = out.getvalue()
        assert "t3-teatree" in message
        known = message.split("Known overlays:", 1)[1]
        assert "teatree" in known  # t3-teatree contains the substring
        known_names = {n.strip() for n in known.split(",")}
        assert "teatree" not in known_names


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestTokenRefMigration:
    """setup slack-bot must migrate tokens when rewriting slack_token_ref to canonical form.

    Regression guard for #2047: when the registry has a stale ``slack_token_ref``
    (e.g. ``teatree/teatree/slack``) and the command computes a different
    canonical ref (``teatree/t3-teatree/slack``), the tokens under the old ref
    must be copied to the new ref slots before the registry is rewritten. Without
    the migration the new canonical ref points at empty pass entries and the
    smoke test immediately fails with a misleading "token may lack im:write"
    error.
    """

    def _seed_old_ref(self, old_ref: str) -> None:
        _seed_overlays({"acme": {"slack_user_id": "U01ABCD1234", "slack_token_ref": old_ref}})

    def test_tokens_under_old_ref_are_migrated_to_canonical_ref(self) -> None:
        """After setup, the canonical ref's slots hold the tokens that lived under the old ref."""
        old_ref = "teatree/old-acme/slack"
        self._seed_old_ref(old_ref)
        store: dict[str, str] = {
            f"{old_ref}-bot": "xoxb-1-oldbot",
            f"{old_ref}-app": "xapp-1-oldapp",
        }

        def fake_read(key: str) -> str:
            return store.get(key, "")

        def fake_write(key: str, value: str) -> bool:
            store[key] = value
            return True

        canonical_ref = "teatree/acme/slack"
        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.token_store.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.write_pass", side_effect=fake_write),
            patch("teatree.cli.slack.setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.setup.write_pass", side_effect=fake_write),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        assert store.get(f"{canonical_ref}-bot") == "xoxb-1-oldbot"
        assert store.get(f"{canonical_ref}-app") == "xapp-1-oldapp"

    def test_rewrite_refused_when_old_ref_is_empty(self) -> None:
        """When no token is stored under the old ref, setup refuses to rewrite to the canonical ref.

        Rewriting to a canonical ref that would also be empty is never safe;
        the user must store tokens first (or run the full create path).
        """
        old_ref = "teatree/old-acme/slack"
        self._seed_old_ref(old_ref)

        def fake_read(_key: str) -> str:
            return ""

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.token_store.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.setup.write_pass", return_value=True),
            patch("teatree.cli.slack.setup.webbrowser.open"),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 1
        assert "no token stored" in result.stdout.lower() or "cannot migrate" in result.stdout.lower()
        assert _overlays()["acme"]["slack_token_ref"] == old_ref

    def test_no_migration_when_ref_already_canonical(self) -> None:
        """When the existing ref is already canonical, setup runs normally without migration.

        A ``slack_app_id`` is present so the command takes the update path
        (no interactive token prompts), making the assertion deterministic.
        The key guard is that the store is NOT modified by migration: the bot
        slot still holds its original value after setup completes.
        """
        canonical_ref = "teatree/acme/slack"
        _seed_overlays(
            {
                "acme": {
                    "slack_user_id": "U01ABCD1234",
                    "slack_token_ref": canonical_ref,
                    "slack_app_id": "A0123456789",
                }
            }
        )
        store: dict[str, str] = {
            f"{canonical_ref}-bot": "xoxb-1-existing",
            f"{canonical_ref}-app": "xapp-1-existing",
        }
        writes: list[str] = []

        def fake_read(key: str) -> str:
            return store.get(key, "")

        def fake_write(key: str, value: str) -> bool:
            writes.append(key)
            store[key] = value
            return True

        with (
            patch("teatree.cli.slack.setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack.token_store.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.write_pass", side_effect=fake_write),
            patch("teatree.cli.slack.setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.setup.write_pass", side_effect=fake_write),
            patch("teatree.cli.slack.setup.webbrowser.open"),
            patch("teatree.cli.slack.setup._smoke_test", return_value=True),
            patch("teatree.cli.slack.setup._run_update_path"),
        ):
            result = _invoke_setup(inputs="", args=["--overlay", "acme", "--skip-smoke-test"])

        assert result.exit_code == 0, result.stdout
        # Migration must not have written anything — the canonical ref was already correct.
        assert not any(canonical_ref in w for w in writes), f"Unexpected migration write(s): {writes}"
        assert store.get(f"{canonical_ref}-bot") == "xoxb-1-existing"
