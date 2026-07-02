"""Config resolution for ``eval_credential`` — the automated-eval credential knob (#2707 reversal).

``eval_credential`` is DB-home: its authoritative tier is the ``ConfigSetting``
store (+ the ``T3_EVAL_CREDENTIAL`` env). The resolver defaults to
``subscription_oauth`` (the post-#2707-reversal default) when no row is set, reads a
stored ``metered_api_key``, lets the env win over the store, and raises LOUD on a
corrupt stored value so the eval lane never silently switches credential kind.
``CONFIG_PATH`` is isolated so the real ``~/.teatree.toml`` never leaks in.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import EvalCredential, get_effective_settings
from teatree.core.models import ConfigSetting


class TestEvalCredentialResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_EVAL_CREDENTIAL", raising=False)

    def test_default_is_subscription_oauth_when_no_row(self) -> None:
        assert get_effective_settings().eval_credential is EvalCredential.SUBSCRIPTION_OAUTH

    def test_stored_metered_api_key(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "metered_api_key")
        assert get_effective_settings().eval_credential is EvalCredential.METERED_API_KEY

    def test_stored_subscription_oauth(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "subscription_oauth")
        assert get_effective_settings().eval_credential is EvalCredential.SUBSCRIPTION_OAUTH

    def test_env_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "subscription_oauth")
        with patch.dict(os.environ, {"T3_EVAL_CREDENTIAL": "metered_api_key"}):
            assert get_effective_settings().eval_credential is EvalCredential.METERED_API_KEY

    def test_corrupt_stored_value_raises(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "on_the_house")
        with pytest.raises(ValueError, match="eval_credential"):
            get_effective_settings()


class TestEvalCredentialParse:
    def test_parses_canonical_and_normalises(self) -> None:
        assert EvalCredential.parse("metered_api_key") is EvalCredential.METERED_API_KEY
        assert EvalCredential.parse("  SUBSCRIPTION_OAUTH  ") is EvalCredential.SUBSCRIPTION_OAUTH

    def test_invalid_value_raises_naming_the_setting(self) -> None:
        with pytest.raises(ValueError, match="eval_credential"):
            EvalCredential.parse("plan_b")
