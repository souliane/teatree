"""Config resolution for ``chinese_models_allowed`` — the OrcaRouter allowlist gate (#2887).

``chinese_models_allowed`` is DB-home: its sole authoritative tier is the
``ConfigSetting`` store (+ the ``T3_CHINESE_MODELS_ALLOWED`` env). The resolver
defaults to ``True`` (teatree's own permissive posture) when no row is set, and an
overlay serving client work under a no-Chinese-models policy overrides it to
``False`` for itself. ``CONFIG_PATH`` is isolated so the real
``~/.teatree.toml`` never leaks in.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


class TestChineseModelsAllowedResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_CHINESE_MODELS_ALLOWED", raising=False)

    def test_default_is_true_when_no_row(self) -> None:
        assert get_effective_settings().chinese_models_allowed is True

    def test_stored_false(self) -> None:
        ConfigSetting.objects.set_value("chinese_models_allowed", value=False)
        assert get_effective_settings().chinese_models_allowed is False

    def test_env_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("chinese_models_allowed", value=True)
        with patch.dict(os.environ, {"T3_CHINESE_MODELS_ALLOWED": "false"}):
            assert get_effective_settings().chinese_models_allowed is False

    def test_corrupt_stored_value_raises(self) -> None:
        ConfigSetting.objects.set_value("chinese_models_allowed", "not-a-bool")
        with pytest.raises(ValueError, match="chinese_models_allowed"):
            get_effective_settings()
