r"""The ``tokens`` management command wiring (``teatree.core.management.commands.tokens``).

The end-to-end ``call_command('tokens')`` behaviour lives in ``tests/test_token_report.py``;
this file exercises the ``Command`` class directly — its framework wiring and that its
``handle`` emits through the machine-output seam and returns the typed rows.
"""

import io
import json
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django_typer.management import TyperCommand

from teatree.core.management.commands.tokens import Command, PickPayload
from teatree.core.models import AnthropicActivePick, AnthropicTokenUsage, ConfigSetting
from teatree.core.models.anthropic_token_usage import TokenHealthReading
from teatree.token_report import TokenAccountPayload


class TokensCommandWiringTest(TestCase):
    def test_command_is_a_typer_command(self) -> None:
        assert issubclass(Command, TyperCommand)

    @staticmethod
    def _run(*, json_output: bool) -> tuple[list[TokenAccountPayload], str, str]:
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        rows = cast("list[TokenAccountPayload]", command.handle(json_output=json_output))
        return rows, command.stdout._out.getvalue(), command.stderr._out.getvalue()

    def test_handle_renders_the_placeholder_on_stderr_when_nothing_is_configured(self) -> None:
        rows, out, err = self._run(json_output=False)
        assert rows == []
        assert out == ""
        assert "No Anthropic accounts configured" in err

    def test_handle_json_puts_an_empty_document_on_stdout(self) -> None:
        rows, out, err = self._run(json_output=True)
        assert rows == []
        assert json.loads(out) == []
        assert err == ""


class TokensPickTest(TestCase):
    """``--pick`` resolves through the routing selector and prints an ENTRY PATH, not a token."""

    _TOKEN = "sk-" + "ant-oat01-never-printed-anywhere"

    @staticmethod
    def _run_pick(*, json_output: bool = False, scope: str = "") -> tuple[PickPayload, str, str]:
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        result = cast("PickPayload", command.handle(json_output=json_output, pick=True, scope=scope))
        return result, command.stdout._out.getvalue(), command.stderr._out.getvalue()

    @staticmethod
    def _configure(*pass_paths: str, scope: str = "") -> None:
        ConfigSetting.objects.set_value("anthropic_oauth_pass_paths", list(pass_paths), scope=scope)

    @staticmethod
    def _record(pass_path: str, *, u7: float) -> None:
        AnthropicTokenUsage.objects.record(
            pass_path,
            TokenHealthReading(
                organization_id="org-1",
                utilization_5h=0.0,
                utilization_7d=u7,
                status_5h="allowed",
                status_7d="allowed",
                reset_5h=None,
                reset_7d=None,
            ),
        )

    def test_pick_prints_the_selected_pass_entry_and_never_a_token(self) -> None:
        self._configure("anthropic/a/oauth")
        self._record("anthropic/a/oauth", u7=0.05)

        with patch("teatree.llm.credentials.read_pass", side_effect=lambda _: self._TOKEN):
            result, out, err = self._run_pick()

        assert out.strip() == "anthropic/a/oauth"
        assert result == {"pass_path": "anthropic/a/oauth", "scope": ""}
        assert self._TOKEN not in out
        assert self._TOKEN not in err

    def test_pick_emits_the_machine_payload_under_json(self) -> None:
        self._configure("anthropic/a/oauth")
        self._record("anthropic/a/oauth", u7=0.05)

        _, out, _ = self._run_pick(json_output=True)

        assert json.loads(out) == {"pass_path": "anthropic/a/oauth", "scope": ""}

    def test_pick_pins_the_sticky_so_a_following_dispatch_shares_the_account(self) -> None:
        self._configure("anthropic/a/oauth")
        self._record("anthropic/a/oauth", u7=0.05)

        self._run_pick()

        assert AnthropicActivePick.objects.pick_for("oauth", "") == "anthropic/a/oauth"

    def test_pick_selects_the_richest_account_not_the_first_configured(self) -> None:
        self._configure("anthropic/a/oauth", "anthropic/b/oauth")
        self._record("anthropic/a/oauth", u7=0.96)
        self._record("anthropic/b/oauth", u7=0.05)

        _, out, _ = self._run_pick()

        assert out.strip() == "anthropic/b/oauth"

    def test_pick_with_nothing_configured_exits_non_zero_naming_the_setting(self) -> None:
        # A `typer.Exit` here would be SWALLOWED by call_command and the process would exit
        # 0 — a real refusal reporting green. `SystemExit` is what actually propagates.
        with pytest.raises(SystemExit) as exc_info:
            self._run_pick()

        assert exc_info.value.code == 1

    def test_the_refusal_propagates_through_call_command(self) -> None:
        # The one that matters: `call_command` SWALLOWS a `typer.Exit`, so a refusal raised
        # that way exits 0 and CI reports green on a real failure. Only `SystemExit` crosses.
        with pytest.raises(SystemExit) as exc_info:
            call_command("tokens", pick=True, scope="nowhere")

        assert exc_info.value.code == 1

    def test_a_successful_pick_through_call_command_returns_the_payload(self) -> None:
        self._configure("anthropic/a/oauth")
        self._record("anthropic/a/oauth", u7=0.05)

        assert call_command("tokens", pick=True) == {"pass_path": "anthropic/a/oauth", "scope": ""}

    def test_the_refusal_names_its_reason_on_stderr_before_raising(self) -> None:
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())

        with pytest.raises(SystemExit):
            command.handle(pick=True, scope="nowhere")

        assert "anthropic_oauth_pass_paths" in command.stderr._out.getvalue()
