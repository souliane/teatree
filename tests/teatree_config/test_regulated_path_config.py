"""Config resolution for the regulated-path model allowlist (#2887).

``regulated_path_model_allowlist`` is a DB-home installation value. An empty list
blocks nothing; a configured list restricts model selection on the regulated path.
"""

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


class TestRegulatedPathConfigResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_allowlist_defaults_empty_when_no_row(self) -> None:
        assert get_effective_settings().regulated_path_model_allowlist == []

    def test_stored_allowlist(self) -> None:
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/", "google/"])
        assert get_effective_settings().regulated_path_model_allowlist == ["anthropic/", "google/"]
