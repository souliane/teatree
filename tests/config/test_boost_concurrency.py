"""The ``boost_concurrency`` pool-refill target (PR-13, #1910).

``boost`` wip keeps ``boost_concurrency`` live loop workers in flight. ``0``
(the default) leaves boost at today's summed-overlay-cap behaviour; a positive
``N`` arms the pool-refill driver. DB-home (#1775): resolves ``env
(T3_BOOST_CONCURRENCY) > per-overlay > global > default``.
"""

from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import get_effective_settings, load_config
from teatree.core.models import ConfigSetting

from ._shared import _write_toml


class TestBoostConcurrencyDefault:
    def test_defaults_to_zero(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.boost_concurrency == 0


class TestBoostConcurrencyDbResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_BOOST_CONCURRENCY", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(
            self.config_path,
            '[teatree]\n\n[overlays.fast]\nclass = "x:Y"\n',
        )
        self.monkeypatch = monkeypatch

    def test_global_db_row(self) -> None:
        ConfigSetting.objects.set_value("boost_concurrency", 4)
        assert get_effective_settings().boost_concurrency == 4

    def test_overlay_scoped_row_wins_over_global(self) -> None:
        ConfigSetting.objects.set_value("boost_concurrency", 2)
        ConfigSetting.objects.set_value("boost_concurrency", 6, scope="fast")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        assert get_effective_settings().boost_concurrency == 6

    def test_env_wins_over_overlay_and_global(self) -> None:
        ConfigSetting.objects.set_value("boost_concurrency", 2)
        ConfigSetting.objects.set_value("boost_concurrency", 6, scope="fast")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        self.monkeypatch.setenv("T3_BOOST_CONCURRENCY", "9")
        assert get_effective_settings().boost_concurrency == 9

    def test_ignores_a_teatree_toml_value(self) -> None:
        # DB-home: a ``[teatree]`` TOML value is ignored on read (only the DB row governs).
        _write_toml(self.config_path, "[teatree]\nboost_concurrency = 7\n")
        assert get_effective_settings().boost_concurrency == 0
