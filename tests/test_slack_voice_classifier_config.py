"""Config resolution for ``slack_voice_classifier_mode`` (#1395).

``slack_voice_classifier_mode`` is DB-home (#1775): its sole authoritative tier
is the ``ConfigSetting`` store (+ ``T3_*`` env). The pre-partition TOML surface
— the flat ``[teatree] slack_voice_classifier_mode`` key and the nested
``[teatree.publish_gates]`` table the issue brief sketched — is ignored on read
now; the mode is staged via ``ConfigSetting.objects.set_value`` instead. This
confirms the resolver defaults to ``WARN`` when no row is set (backward-compat),
reads a stored ``strict`` / ``off`` row, and surfaces a clean ``ValueError`` on a
corrupt stored value so a silent mode downgrade never lands. ``CONFIG_PATH`` is
isolated so the real ``~/.teatree.toml`` never leaks in.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.backends.slack.voice_classifier import ClassifierMode
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


class TestVoiceClassifierModeResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_default_is_warn_when_no_row(self) -> None:
        """No DB row and no config file → the dataclass default ``WARN``."""
        assert get_effective_settings().slack_voice_classifier_mode is ClassifierMode.WARN

    def test_stored_strict(self) -> None:
        ConfigSetting.objects.set_value("slack_voice_classifier_mode", "strict")
        assert get_effective_settings().slack_voice_classifier_mode is ClassifierMode.STRICT

    def test_stored_off(self) -> None:
        ConfigSetting.objects.set_value("slack_voice_classifier_mode", "off")
        assert get_effective_settings().slack_voice_classifier_mode is ClassifierMode.OFF

    def test_corrupt_stored_value_raises(self) -> None:
        """A corrupt stored value is raised LOUD, never silently downgraded."""
        ConfigSetting.objects.set_value("slack_voice_classifier_mode", "loud")
        with pytest.raises(ValueError, match="slack_voice_classifier_mode"):
            get_effective_settings()
