"""Config resolution for the regulated-path allowlist gate (#2887).

``enforce_regulated_path`` and ``regulated_path_model_allowlist`` are DB-home:
their sole authoritative tier is the ``ConfigSetting`` store (+ the
``T3_ENFORCE_REGULATED_PATH`` env for the gate). Default: the gate is ``False``
(the teatree factory lane carries no regulated data and runs unrestricted) and the
allowlist is empty. A regulated overlay (a future regulated lane carrying client/
bank data under EU data-residency & regulatory compliance) sets the gate ``True`` and
enumerates the compliant models in the allowlist.
"""

import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


class TestRegulatedPathConfigResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ENFORCE_REGULATED_PATH", raising=False)

    def test_gate_defaults_false_when_no_row(self) -> None:
        assert get_effective_settings().enforce_regulated_path is False

    def test_allowlist_defaults_empty_when_no_row(self) -> None:
        assert get_effective_settings().regulated_path_model_allowlist == []

    def test_stored_gate_true(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        assert get_effective_settings().enforce_regulated_path is True

    def test_stored_allowlist(self) -> None:
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/", "google/"])
        assert get_effective_settings().regulated_path_model_allowlist == ["anthropic/", "google/"]

    def test_env_wins_over_store_for_the_gate(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=False)
        with patch.dict(os.environ, {"T3_ENFORCE_REGULATED_PATH": "true"}):
            assert get_effective_settings().enforce_regulated_path is True

    def test_corrupt_stored_gate_value_raises(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", "not-a-bool")
        with pytest.raises(ValueError, match="enforce_regulated_path"):
            get_effective_settings()
