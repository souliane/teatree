"""``t3 eval run --credential`` — the per-run eval-credential override.

The eval lane's credential is ``agent_harness_provider``'s call, so the flag pins
that provider for THIS process only: an operator whose loop rides the subscription
can still run a one-off metered eval without mutating stored config. The pin lands
on the env tier, so ``resolve_eval_credential`` (and every eval chokepoint through
it) picks it up with no threading, and the eval container recovers the same kind
from the one credential var the host forwards.
"""

import os
import re
from collections.abc import Iterator

import pytest
import typer
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.eval.app import eval_app
from teatree.cli.eval.app_helpers import PROVIDER_ENV_VAR, apply_credential_override
from teatree.credential_config import resolve_eval_credential
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _restore_provider_pin() -> Iterator[None]:
    """Save/restore the provider pin around every test in this module.

    ``apply_credential_override`` pins on the ENV tier by design, so a test that
    exercises it mutates ``os.environ`` directly. ``monkeypatch.delenv`` cannot undo
    that: on a var that was absent it records nothing to restore, so the pin would
    survive into unrelated eval tests and silently switch their credential. Restoring
    explicitly keeps the override local to the test that sets it.
    """
    previous = os.environ.get(PROVIDER_ENV_VAR)
    os.environ.pop(PROVIDER_ENV_VAR, None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(PROVIDER_ENV_VAR, None)
        else:
            os.environ[PROVIDER_ENV_VAR] = previous


class TestApplyCredentialOverride:
    def test_no_flag_leaves_the_env_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PROVIDER_ENV_VAR, raising=False)
        apply_credential_override(None)
        assert PROVIDER_ENV_VAR not in os.environ

    def test_each_accepted_value_pins_the_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("subscription_oauth", "api_key"):
            monkeypatch.delenv(PROVIDER_ENV_VAR, raising=False)
            apply_credential_override(value)
            assert os.environ[PROVIDER_ENV_VAR] == value

    def test_an_unknown_value_exits_2_naming_the_choices(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(PROVIDER_ENV_VAR, raising=False)
        with pytest.raises(typer.Exit) as exc:
            apply_credential_override("metered_api_key")
        assert exc.value.exit_code == 2
        err = capsys.readouterr().err
        assert "subscription_oauth" in err
        assert "api_key" in err
        assert PROVIDER_ENV_VAR not in os.environ, "a rejected value must not pin anything"


class TestCredentialOverrideReachesTheSeam(TestCase):
    """The flag's pin is what ``resolve_eval_credential`` reads — no threading needed."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The provider pin itself is handled by the module-level save/restore fixture.
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_EVAL_IN_CONTAINER", raising=False)

    def test_api_key_override_switches_the_resolved_credential(self) -> None:
        assert isinstance(resolve_eval_credential(), AnthropicSubscriptionCredential)
        apply_credential_override("api_key")
        assert isinstance(resolve_eval_credential(), AnthropicApiKeyCredential)

    def test_subscription_override_keeps_the_oauth_credential(self) -> None:
        apply_credential_override("subscription_oauth")
        assert isinstance(resolve_eval_credential(), AnthropicSubscriptionCredential)


class TestRunCommandExposesTheFlag:
    def test_help_lists_the_credential_flag_and_its_choices(self) -> None:
        result = CliRunner().invoke(eval_app, ["run", "--help"])
        assert result.exit_code == 0
        # rich wraps the option row across lines inside a box; flatten the frame and
        # whitespace so the assertion reads the help TEXT, not its rendering width.
        rendered = " ".join(_ANSI.sub("", result.output).replace("│", " ").split())
        assert "--credential" in rendered
        assert "subscription_oauth | api_key" in rendered
