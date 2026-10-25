import os
from unittest.mock import patch

from django.db import DatabaseError

from teatree.cli.setup.agent_tool_credentials import AgentTool, StrictPluginGate
from teatree.core.models import ConfigSetting
from tests._hermetic_agent_host import HermeticHostCase

_CLAUDE_FAILS = {AgentTool.CLAUDE: False, AgentTool.CODEX: True}
_CODEX_FAILS = {AgentTool.CLAUDE: True, AgentTool.CODEX: False}
_BOTH_FAIL = {AgentTool.CLAUDE: False, AgentTool.CODEX: False}


class GateCase(HermeticHostCase):
    def passes(self, registered: dict[AgentTool, bool]) -> bool:
        return StrictPluginGate().passes(registered)


class TestNoCredentialAnywhere(GateCase):
    def test_a_failing_claude_registration_blocks(self) -> None:
        assert not self.passes(_CLAUDE_FAILS)

    def test_a_failing_codex_registration_blocks(self) -> None:
        assert not self.passes(_CODEX_FAILS)

    def test_both_registered_passes(self) -> None:
        assert self.passes({AgentTool.CLAUDE: True, AgentTool.CODEX: True})

    def test_the_error_names_every_blocking_tool(self) -> None:
        with patch("typer.echo") as echo:
            assert not self.passes(_BOTH_FAIL)

        messages = [call.args[0] for call in echo.call_args_list if call.kwargs.get("err")]
        assert len(messages) == 1
        assert "claude" in messages[0]
        assert "codex" in messages[0]


class TestOnlyOneToolCredentialed(GateCase):
    def test_a_credentialed_claude_passes_when_only_codex_fails_and_warns(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"

        with patch("typer.echo") as echo:
            assert self.passes(_CODEX_FAILS)

        warned = [call.args[0] for call in echo.call_args_list if not call.kwargs.get("err")]
        assert any(line.startswith("WARN") and "codex" in line for line in warned)
        assert not any(call.kwargs.get("err") for call in echo.call_args_list)

    def test_a_credentialed_claude_blocks_when_claude_fails(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"

        assert not self.passes(_CLAUDE_FAILS)

    def test_a_credentialed_codex_passes_when_only_claude_fails(self) -> None:
        (self.codex_home / "auth.json").write_text("{}", encoding="utf-8")

        assert self.passes(_CLAUDE_FAILS)

    def test_a_credentialed_codex_blocks_and_names_codex_on_stderr(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        (self.codex_home / "auth.json").write_text("{}", encoding="utf-8")

        with patch("typer.echo") as echo:
            assert not self.passes(_CODEX_FAILS)

        stderr = [call.args[0] for call in echo.call_args_list if call.kwargs.get("err")]
        assert len(stderr) == 1
        assert "codex" in stderr[0]
        assert "claude" not in stderr[0]

    def test_nothing_is_echoed_when_every_registration_succeeds(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"

        with patch("typer.echo") as echo:
            assert self.passes({AgentTool.CLAUDE: True, AgentTool.CODEX: True})

        echo.assert_not_called()


class TestEachCredentialSourceMakesItsToolCredentialed(GateCase):
    def _assert_claude_only(self) -> None:
        assert self.passes(_CODEX_FAILS), "the Claude source did not mark Claude credentialed"
        assert not self.passes(_CLAUDE_FAILS), "the Claude source also relaxed the Claude gate"

    def _assert_codex_only(self) -> None:
        assert self.passes(_CLAUDE_FAILS), "the Codex source did not mark Codex credentialed"
        assert not self.passes(_CODEX_FAILS), "the Codex source also relaxed the Codex gate"

    def test_the_api_key_env_var(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        self._assert_claude_only()

    def test_the_oauth_token_env_var(self) -> None:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "oauth-test"
        self._assert_claude_only()

    def test_the_claude_credentials_file(self) -> None:
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        self._assert_claude_only()

    def test_an_oauth_routing_row_at_global_scope(self) -> None:
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", ["claude/oauth/main"])
        self._assert_claude_only()

    def test_an_api_key_routing_row_at_overlay_scope(self) -> None:
        ConfigSetting.objects.set_value("anthropic_api_key_pass_paths", ["claude/api/main"], scope="acme")
        self._assert_claude_only()

    def test_the_codex_home_auth_file(self) -> None:
        (self.codex_home / "auth.json").write_text("{}", encoding="utf-8")
        self._assert_codex_only()

    def test_the_private_codex_home_auth_file(self) -> None:
        (self.private_codex_home / "auth.json").write_text("{}", encoding="utf-8")
        self._assert_codex_only()

    def test_the_codex_pass_file_is_detected_by_listing(self) -> None:
        pass_directory = self.password_store / "teatree" / "codex"
        pass_directory.mkdir(parents=True)
        (pass_directory / "auth-json-b64.gpg").write_bytes(b"not-decrypted")
        self._assert_codex_only()


class TestSourcesThatDoNotCount(GateCase):
    def _assert_nothing_credentialed(self) -> None:
        assert not self.passes(_CLAUDE_FAILS)
        assert not self.passes(_CODEX_FAILS)

    def test_an_empty_env_var_is_not_a_credential(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = ""
        self._assert_nothing_credentialed()

    def test_an_empty_routing_list_is_not_a_credential(self) -> None:
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", [])
        self._assert_nothing_credentialed()

    def test_a_routing_row_for_an_unrelated_key_is_not_a_credential(self) -> None:
        ConfigSetting.objects.set_value("review_exempt_repos", ["acme/legacy"])
        self._assert_nothing_credentialed()

    def test_another_entry_in_the_codex_pass_directory_is_not_a_credential(self) -> None:
        pass_directory = self.password_store / "teatree" / "codex"
        pass_directory.mkdir(parents=True)
        (pass_directory / "unrelated.gpg").write_bytes(b"x")
        self._assert_nothing_credentialed()

    def test_a_claude_home_entry_that_is_a_regular_file_is_not_a_credential(self) -> None:
        (self.home / ".claude").write_text("a file, not a directory", encoding="utf-8")
        self._assert_nothing_credentialed()

    def test_a_codex_home_that_is_a_regular_file_is_not_a_credential(self) -> None:
        codex_file = self.codex_home / "not-a-home"
        codex_file.write_text("a file, not a directory", encoding="utf-8")
        os.environ["CODEX_HOME"] = str(codex_file)
        self._assert_nothing_credentialed()


class TestAnUnreadableSourceCountsAsHeld(GateCase):
    def test_a_credentials_path_that_cannot_be_statted(self) -> None:
        loop = self.home / ".claude" / ".credentials.json"
        loop.parent.mkdir()
        loop.symlink_to(loop)

        assert self.passes(_CODEX_FAILS)
        assert not self.passes(_CLAUDE_FAILS)

    def test_a_codex_pass_prefix_that_is_not_a_directory(self) -> None:
        (self.password_store / "teatree").mkdir()
        (self.password_store / "teatree" / "codex").write_text("a file, not a directory", encoding="utf-8")

        assert self.passes(_CLAUDE_FAILS)
        assert not self.passes(_CODEX_FAILS)

    def test_a_routing_read_that_fails(self) -> None:
        def raise_database_error(*_args: object, **_kwargs: object) -> object:
            raise DatabaseError

        with patch.object(ConfigSetting.objects, "filter", raise_database_error):
            assert self.passes(_CODEX_FAILS)
            assert not self.passes(_CLAUDE_FAILS)
