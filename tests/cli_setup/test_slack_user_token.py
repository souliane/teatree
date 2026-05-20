"""Tests for ``t3 setup slack-user-token`` — xoxp re-scoping walkthrough."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from teatree.cli.setup import setup_app
from teatree.cli.slack_user_token_setup import (
    _USER_TOKEN_RE,
    REQUIRED_USER_SCOPES,
    USER_TOKEN_PASS_KEY,
    TokenScopeError,
    _store_and_verify,
    added_scopes,
    fetch_token_scopes,
    missing_scopes,
)


class TestUserTokenPattern:
    @pytest.mark.parametrize("value", ["xoxp-1234-abcDEF", "xoxp-0-x"])
    def test_accepts_valid_xoxp(self, value: str) -> None:
        assert _USER_TOKEN_RE.match(value)

    @pytest.mark.parametrize("value", ["xoxb-1-a", "abc", "xoxp", " xoxp-1"])
    def test_rejects_invalid(self, value: str) -> None:
        assert _USER_TOKEN_RE.match(value) is None


class TestRequiredUserScopes:
    def test_includes_new_connect_write_scopes(self) -> None:
        for scope in ("reactions:write", "chat:write.public", "chat:write.customize"):
            assert scope in REQUIRED_USER_SCOPES

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
        from teatree.cli.slack_setup import _USER_SCOPES  # noqa: PLC0415

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
        with patch("teatree.cli.slack_user_token_setup.httpx.post", return_value=response):
            assert fetch_token_scopes("xoxp-test") == ["chat:write", "reactions:write", "users:read"]

    def test_raises_when_slack_returns_not_ok(self) -> None:
        response = _make_response(body={"ok": False, "error": "invalid_auth"})
        with (
            patch("teatree.cli.slack_user_token_setup.httpx.post", return_value=response),
            pytest.raises(TokenScopeError, match="invalid_auth"),
        ):
            fetch_token_scopes("xoxp-bad")

    def test_empty_header_yields_empty_list(self) -> None:
        response = _make_response(headers={})
        with patch("teatree.cli.slack_user_token_setup.httpx.post", return_value=response):
            assert fetch_token_scopes("xoxp-test") == []


class TestStoreAndVerify:
    def _patch_fetch(self, scopes: list[str]) -> Any:
        return patch("teatree.cli.slack_user_token_setup.fetch_token_scopes", return_value=scopes)

    def test_writes_token_when_all_scopes_granted(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        with (
            self._patch_fetch(granted),
            patch("teatree.cli.slack_user_token_setup.write_pass", return_value=True) as write,
        ):
            actual, added = _store_and_verify("xoxp-good", previous_scopes=["chat:write"])
        assert actual == granted
        assert "reactions:write" in added
        write.assert_called_once_with(USER_TOKEN_PASS_KEY, "xoxp-good")

    def test_raises_when_a_scope_is_missing(self) -> None:
        granted = [s for s in REQUIRED_USER_SCOPES if s != "reactions:write"]
        with self._patch_fetch(granted), pytest.raises(TokenScopeError, match="reactions:write"):
            _store_and_verify("xoxp-bad", previous_scopes=[])

    def test_raises_when_pass_write_fails(self) -> None:
        granted = list(REQUIRED_USER_SCOPES)
        with (
            self._patch_fetch(granted),
            patch("teatree.cli.slack_user_token_setup.write_pass", return_value=False),
            pytest.raises(TokenScopeError, match="pass insert"),
        ):
            _store_and_verify("xoxp-good", previous_scopes=[])


class TestCliWalkthrough:
    def _run(self, args: list[str], inputs: str) -> Any:
        runner = CliRunner()
        return runner.invoke(setup_app, ["slack-user-token", *args], input=inputs)

    def test_happy_path_stores_token_and_reports_added_scopes(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.acme]\nslack_app_id = "A0B38QGGF18"\n', encoding="utf-8")
        granted = list(REQUIRED_USER_SCOPES)
        with (
            patch("teatree.cli.slack_user_token_setup.read_pass", return_value=""),
            patch("teatree.cli.slack_user_token_setup.write_pass", return_value=True),
            patch("teatree.cli.slack_user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack_user_token_setup.webbrowser.open"),
        ):
            result = self._run(["--config", str(config)], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert f"{USER_TOKEN_PASS_KEY} updated with {len(granted)} scope(s)" in result.output

    def test_missing_scope_fails_loudly(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        granted = [s for s in REQUIRED_USER_SCOPES if s != "chat:write.public"]
        with (
            patch("teatree.cli.slack_user_token_setup.read_pass", return_value=""),
            patch("teatree.cli.slack_user_token_setup.write_pass", return_value=True),
            patch("teatree.cli.slack_user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack_user_token_setup.webbrowser.open"),
        ):
            result = self._run(["--config", str(config)], inputs="xoxp-new-token\n")
        assert result.exit_code == 1
        assert "chat:write.public" in result.output

    def test_default_mode_prompts_before_overwriting_existing(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        with (
            patch("teatree.cli.slack_user_token_setup.read_pass", return_value="xoxp-old"),
            patch(
                "teatree.cli.slack_user_token_setup.fetch_token_scopes",
                return_value=list(REQUIRED_USER_SCOPES),
            ),
            patch("teatree.cli.slack_user_token_setup.webbrowser.open"),
        ):
            result = self._run(["--config", str(config)], inputs="n\n")
        assert result.exit_code == 1
        assert "Aborted" in result.output

    def test_scope_list_printed_before_overwrite_prompt(self, tmp_path: Path) -> None:
        """UX guard: the user must see the scope set before being asked to overwrite."""
        config = tmp_path / "teatree.toml"
        with (
            patch("teatree.cli.slack_user_token_setup.read_pass", return_value="xoxp-old"),
            patch(
                "teatree.cli.slack_user_token_setup.fetch_token_scopes",
                return_value=list(REQUIRED_USER_SCOPES),
            ),
            patch("teatree.cli.slack_user_token_setup.webbrowser.open"),
        ):
            result = self._run(["--config", str(config)], inputs="n\n")
        scope_section_at = result.output.find("Requested user scopes")
        overwrite_prompt_at = result.output.find("already exists. Overwrite")
        assert scope_section_at >= 0
        assert overwrite_prompt_at > scope_section_at

    def test_reset_overwrites_without_prompting(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        granted = list(REQUIRED_USER_SCOPES)
        with (
            patch("teatree.cli.slack_user_token_setup.read_pass", return_value="xoxp-old"),
            patch("teatree.cli.slack_user_token_setup.write_pass", return_value=True),
            patch("teatree.cli.slack_user_token_setup.fetch_token_scopes", return_value=granted),
            patch("teatree.cli.slack_user_token_setup.webbrowser.open"),
        ):
            result = self._run(["--reset", "--config", str(config)], inputs="xoxp-new-token\n")
        assert result.exit_code == 0, result.output
        assert "Aborted" not in result.output
