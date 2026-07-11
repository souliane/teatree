"""Tests for ``t3 setup slack-user-token`` — xoxp re-scoping walkthrough.

The command resolves an overlay's ``slack_app_id`` from the DB ``overlays``
registry, so the CLI-walkthrough classes are DB-backed and seed via
:func:`_seed_overlays`.
"""

from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from teatree.cli.setup import setup_app
from teatree.cli.slack.user_token_setup import (
    _USER_TOKEN_RE,
    BOT_TOKEN_PASS_KEY,
    REQUIRED_USER_SCOPES,
    USER_TOKEN_PASS_KEY,
    TokenScopeError,
    _derive_app_id_from_bot,
    _detect_and_backup_xoxb_mis_install,
    _store_and_verify,
    added_scopes,
    fetch_token_scopes,
    missing_scopes,
)
from teatree.core.models import ConfigSetting


def _seed_overlays(overlays: dict[str, dict]) -> None:
    ConfigSetting.objects.set_value("overlays", overlays)


class TestUserTokenPattern:
    @pytest.mark.parametrize("value", ["xoxp-1234-abcDEF", "xoxp-0-x"])
    def test_accepts_valid_xoxp(self, value: str) -> None:
        assert _USER_TOKEN_RE.match(value)

    @pytest.mark.parametrize("value", ["xoxb-1-a", "abc", "xoxp", " xoxp-1"])
    def test_rejects_invalid(self, value: str) -> None:
        assert _USER_TOKEN_RE.match(value) is None


class TestRequiredUserScopes:
    def test_includes_connect_write_and_reaction_scopes(self) -> None:
        for scope in ("reactions:write", "reactions:read", "chat:write"):
            assert scope in REQUIRED_USER_SCOPES

    def test_excludes_bot_only_scopes(self) -> None:
        from teatree.cli.slack.setup import _BOT_ONLY_SCOPES  # noqa: PLC0415 — scoped import inside the test method

        assert _BOT_ONLY_SCOPES.isdisjoint(REQUIRED_USER_SCOPES)

    def test_preserves_existing_user_capabilities(self) -> None:
        for scope in ("chat:write", "users:read", "users:read.email", "canvases:write"):
            assert scope in REQUIRED_USER_SCOPES

    def test_no_duplicate_scopes(self) -> None:
        assert len(REQUIRED_USER_SCOPES) == len(set(REQUIRED_USER_SCOPES))

    def test_matches_manifest_scope_set_exactly(self) -> None:
        """Drift guard between manifest and verifier scope sets.

        Slack only grants what the manifest's ``_USER_SCOPES`` declares, so
        the verifier's ``REQUIRED_USER_SCOPES`` must equal that set. Any
        divergence either (a) trips the missing-scope check on every run or
        (b) silently approves under-scoped tokens.
        """
        from teatree.cli.slack.setup import _USER_SCOPES  # noqa: PLC0415 — scoped import inside the test method

        assert set(REQUIRED_USER_SCOPES) == set(_USER_SCOPES)


class TestScopeHelpers:
    def test_missing_scopes_returns_required_minus_actual(self) -> None:
        assert missing_scopes(["chat:write"], ["chat:write", "reactions:write"]) == ["reactions:write"]

    def test_missing_scopes_empty_when_superset(self) -> None:
        assert missing_scopes(["a", "b", "c"], ["a", "b"]) == []

    def test_added_scopes_returns_new_grants(self) -> None:
        assert added_scopes(["a", "b", "c"], ["a"]) == ["b", "c"]


def _make_response(headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        headers=headers or {},
        json=body or {"ok": True, "user": "u"},
        request=httpx.Request("POST", "https://slack.com/api/auth.test"),
    )


class TestFetchTokenScopes:
    def test_returns_sorted_scopes_from_header(self) -> None:
        response = _make_response(headers={"x-oauth-scopes": "chat:write, reactions:write, users:read"})
        with patch("teatree.cli.slack.user_token_setup.httpx.post", return_value=response):
            assert fetch_token_scopes("xoxp-test") == ["chat:write", "reactions:write", "users:read"]

    def test_raises_when_slack_returns_not_ok(self) -> None:
        response = _make_response(body={"ok": False, "error": "invalid_auth"})
        with (
            patch("teatree.cli.slack.user_token_setup.httpx.post", return_value=response),
            pytest.raises(TokenScopeError, match="invalid_auth"),
        ):
            fetch_token_scopes("xoxp-bad")

    def test_empty_header_yields_empty_list(self) -> None:
        response = _make_response(headers={})
        with patch("teatree.cli.slack.user_token_setup.httpx.post", return_value=response):
            assert fetch_token_scopes("xoxp-test") == []


class TestStoreAndVerify:
    def _patch_fetch(self, scopes: list[str]) -> Any:
        return patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=scopes)

    def test_writes_token_when_all_scopes_granted(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        with (
            self._patch_fetch(granted),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
        ):
            actual, added = _store_and_verify("xoxp-good", previous_scopes=["chat:write"], echo=lambda _m: None)
        assert actual == granted
        assert "reactions:write" in added
        write.assert_called_once_with(USER_TOKEN_PASS_KEY, "xoxp-good")

    def test_refuses_to_write_a_bot_token_into_the_user_slot(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        with (
            self._patch_fetch(granted),
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-prioruser"),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
            pytest.raises(TokenScopeError, match="must start with 'xoxp-'"),
        ):
            _store_and_verify("xoxb-WRONG", previous_scopes=[], echo=lambda _m: None)
        write.assert_not_called()

    def test_backs_up_prior_token_before_overwrite(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        writes: list[tuple[str, str]] = []
        with (
            self._patch_fetch(granted),
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-prioruser"),
            patch(
                "teatree.cli.slack.token_store.write_pass",
                side_effect=lambda key, value: writes.append((key, value)) or True,
            ),
        ):
            _store_and_verify("xoxp-new", previous_scopes=[], echo=lambda _m: None)
        backup_writes = [w for w in writes if w[0].startswith(f"{USER_TOKEN_PASS_KEY}.bak-")]
        assert backup_writes == [(backup_writes[0][0], "xoxp-prioruser")]
        assert (USER_TOKEN_PASS_KEY, "xoxp-new") in writes

    def test_raises_when_a_scope_is_missing(self) -> None:
        granted = [s for s in REQUIRED_USER_SCOPES if s != "reactions:write"]
        with self._patch_fetch(granted), pytest.raises(TokenScopeError, match="reactions:write"):
            _store_and_verify("xoxp-bad", previous_scopes=[], echo=lambda _m: None)

    def test_raises_when_pass_write_fails(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        with (
            self._patch_fetch(granted),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=False),
            pytest.raises(TokenScopeError, match="pass insert"),
        ):
            _store_and_verify("xoxp-good", previous_scopes=[], echo=lambda _m: None)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestCliWalkthrough:
    def _run(self, args: list[str], inputs: str) -> Any:
        runner = CliRunner()
        return runner.invoke(setup_app, ["slack-user-token", *args], input=inputs)

    def test_happy_path_stores_token_and_reports_added_scopes(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A0DEMOAPP01"}})
        granted = list(REQUIRED_USER_SCOPES)
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open"),
        ):
            result = self._run([], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert f"{USER_TOKEN_PASS_KEY} updated with {len(granted)} scope(s)" in result.output

    def test_missing_scope_fails_loudly(self) -> None:
        granted = [s for s in REQUIRED_USER_SCOPES if s != "reactions:write"]
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open"),
        ):
            result = self._run([], inputs="xoxp-new-token\n")
        assert result.exit_code == 1
        assert "reactions:write" in result.output

    def test_default_mode_prompts_before_overwriting_existing(self) -> None:
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value="xoxp-old"),
            patch(
                "teatree.cli.slack.user_token_setup.fetch_token_scopes",
                return_value=list(REQUIRED_USER_SCOPES),
            ),
            patch("teatree.cli.slack.user_token_setup._derive_app_id_from_bot", return_value=""),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open"),
        ):
            result = self._run([], inputs="n\n")
        assert result.exit_code == 1
        assert "Aborted" in result.output

    def test_scope_list_printed_before_overwrite_prompt(self) -> None:
        """UX guard: the user must see the scope set before being asked to overwrite."""
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value="xoxp-old"),
            patch(
                "teatree.cli.slack.user_token_setup.fetch_token_scopes",
                return_value=list(REQUIRED_USER_SCOPES),
            ),
            patch("teatree.cli.slack.user_token_setup._derive_app_id_from_bot", return_value=""),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open"),
        ):
            result = self._run([], inputs="n\n")
        scope_section_at = result.output.find("Requested user scopes")
        overwrite_prompt_at = result.output.find("already exists. Overwrite")
        assert scope_section_at >= 0
        assert overwrite_prompt_at > scope_section_at

    def test_reset_overwrites_without_prompting(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value="xoxp-old"),
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-old"),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack.user_token_setup._derive_app_id_from_bot", return_value=""),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open"),
        ):
            result = self._run(["--reset"], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert "Aborted" not in result.output


class TestDetectAndBackupXoxbMisInstall:
    def test_xoxb_at_user_token_path_backed_up_when_bot_slot_empty(self) -> None:
        def fake_read(key: str) -> str:
            return {USER_TOKEN_PASS_KEY: "xoxb-1-abc", BOT_TOKEN_PASS_KEY: ""}.get(key, "")

        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
        ):
            messages: list[str] = []
            _detect_and_backup_xoxb_mis_install(echo=messages.append)
        write.assert_called_once_with(BOT_TOKEN_PASS_KEY, "xoxb-1-abc")
        assert any("bot token mis-install detected" in m for m in messages)

    def test_xoxb_skips_backup_when_bot_slot_already_matches(self) -> None:
        def fake_read(key: str) -> str:
            return {USER_TOKEN_PASS_KEY: "xoxb-1-abc", BOT_TOKEN_PASS_KEY: "xoxb-1-abc"}.get(key, "")

        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
        ):
            _detect_and_backup_xoxb_mis_install(echo=lambda _msg: None)
        write.assert_not_called()

    def test_xoxp_at_user_token_path_does_nothing(self) -> None:
        def fake_read(key: str) -> str:
            return {USER_TOKEN_PASS_KEY: "xoxp-1-good", BOT_TOKEN_PASS_KEY: ""}.get(key, "")

        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
        ):
            _detect_and_backup_xoxb_mis_install(echo=lambda _msg: None)
        write.assert_not_called()

    def test_xoxb_backup_preserves_stale_bot_slot_before_overwrite(self) -> None:
        def fake_read(key: str) -> str:
            return {USER_TOKEN_PASS_KEY: "xoxb-1-new", BOT_TOKEN_PASS_KEY: "xoxb-1-stale"}.get(key, "")

        writes: list[tuple[str, str]] = []
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.read_pass", side_effect=fake_read),
            patch(
                "teatree.cli.slack.token_store.write_pass",
                side_effect=lambda key, value: writes.append((key, value)) or True,
            ),
        ):
            messages: list[str] = []
            _detect_and_backup_xoxb_mis_install(echo=messages.append)
        # The stale bot-slot value is preserved to a timestamped backup before
        # the new value overwrites it, so a clobber stays recoverable.
        backup_writes = [w for w in writes if w[0].startswith(f"{BOT_TOKEN_PASS_KEY}.bak-")]
        assert backup_writes == [(backup_writes[0][0], "xoxb-1-stale")]
        assert (BOT_TOKEN_PASS_KEY, "xoxb-1-new") in writes
        assert any("bot token mis-install detected" in m for m in messages)


def _slack_response(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://slack.com/api/auth.test"),
    )


class TestDeriveAppIdFromBot:
    def test_returns_app_id_when_auth_test_and_bots_info_succeed(self) -> None:
        responses = [
            _slack_response({"ok": True, "bot_id": "B12345"}),
            _slack_response({"ok": True, "bot": {"app_id": "A99999"}}),
        ]
        with patch("teatree.cli.slack.user_token_setup.httpx.post", side_effect=responses):
            assert _derive_app_id_from_bot("xoxb-anything") == "A99999"

    def test_returns_empty_when_auth_test_fails(self) -> None:
        response = _slack_response({"ok": False, "error": "invalid_auth"})
        with patch("teatree.cli.slack.user_token_setup.httpx.post", return_value=response):
            assert _derive_app_id_from_bot("xoxb-bad") == ""

    def test_returns_empty_when_bots_info_fails(self) -> None:
        responses = [
            _slack_response({"ok": True, "bot_id": "B12345"}),
            _slack_response({"ok": False, "error": "bot_not_found"}),
        ]
        with patch("teatree.cli.slack.user_token_setup.httpx.post", side_effect=responses):
            assert _derive_app_id_from_bot("xoxb-anything") == ""

    def test_returns_empty_when_no_bot_id_in_auth_test(self) -> None:
        response = _slack_response({"ok": True})
        with patch("teatree.cli.slack.user_token_setup.httpx.post", return_value=response):
            assert _derive_app_id_from_bot("xoxb-anything") == ""

    def test_returns_empty_when_no_app_id_in_bots_info(self) -> None:
        responses = [
            _slack_response({"ok": True, "bot_id": "B12345"}),
            _slack_response({"ok": True, "bot": {}}),
        ]
        with patch("teatree.cli.slack.user_token_setup.httpx.post", side_effect=responses):
            assert _derive_app_id_from_bot("xoxb-anything") == ""

    def test_returns_empty_on_http_error(self) -> None:
        with patch(
            "teatree.cli.slack.user_token_setup.httpx.post",
            side_effect=httpx.ConnectError("boom"),
        ):
            assert _derive_app_id_from_bot("xoxb-anything") == ""

    def test_returns_empty_when_token_is_blank(self) -> None:
        assert _derive_app_id_from_bot("") == ""


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestNewIntegrationsInWalkthrough:
    """Wires the new helpers into the CLI walkthrough."""

    def _run(self, args: list[str], inputs: str) -> Any:
        runner = CliRunner()
        return runner.invoke(setup_app, ["slack-user-token", *args], input=inputs)

    def test_xoxb_in_user_token_slot_triggers_backup_before_reinstall(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A0DEMOAPP01"}})
        granted = list(REQUIRED_USER_SCOPES)

        def fake_read(key: str) -> str:
            return {USER_TOKEN_PASS_KEY: "xoxb-1-abc", BOT_TOKEN_PASS_KEY: ""}.get(key, "")

        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.read_pass", side_effect=fake_read),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
            patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open"),
        ):
            result = self._run(["--reset"], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert "bot token mis-install detected" in result.output
        write.assert_any_call(BOT_TOKEN_PASS_KEY, "xoxb-1-abc")

    def test_app_id_derived_when_registry_empty_and_token_present(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        opens: list[str] = []
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value="xoxp-existing"),
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-existing"),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack.user_token_setup._derive_app_id_from_bot", return_value="AABCDEF"),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open", side_effect=opens.append),
        ):
            result = self._run(["--reset"], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert opens == ["https://api.slack.com/apps/AABCDEF/oauth"]

    def test_app_id_derivation_failure_prints_fallback(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        opens: list[str] = []
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value="xoxp-existing"),
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-existing"),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack.user_token_setup._derive_app_id_from_bot", return_value=""),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open", side_effect=opens.append),
        ):
            result = self._run(["--reset"], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert "https://api.slack.com/apps" in result.output
        assert opens == []

    def test_install_url_targets_oauth_section_when_app_id_known(self) -> None:
        _seed_overlays({"acme": {"slack_app_id": "A0DEMOAPP01"}})
        granted = list(REQUIRED_USER_SCOPES)
        opens: list[str] = []
        with (
            patch("teatree.cli.slack.user_token_setup.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True),
            patch("teatree.cli.slack.user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack.user_token_setup.webbrowser.open", side_effect=opens.append),
        ):
            result = self._run([], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert opens == ["https://api.slack.com/apps/A0DEMOAPP01/oauth"]
