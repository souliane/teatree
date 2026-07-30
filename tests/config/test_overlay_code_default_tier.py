# test-path: cross-cutting
"""The overlay-code-default tier in the effective-settings chain (#36).

A genuinely-constant, public setting is promoted to a Python overlay code
default (an ``OverlayConfig`` field / the overlay's ``overlay_settings.py``),
still DB-overridable. The resolver inserts that tier BETWEEN the DB(global) row
and the ``UserSettings`` dataclass default, so per promoted key:

    env -> DB(overlay) -> DB(global) -> overlay code default -> dataclass default

``review_skill`` is the observable pilot: its dataclass default is ``""`` while
the public teatree overlay's code default is ``"ac-reviewing-codebase"`` — so a
row-less resolution proves the code default wins over the dataclass default, and
a row at any scope proves the DB still overrides it.

Integration-first: real ``ConfigSetting`` rows against the real DB, the real
``t3-teatree`` overlay active via ``T3_OVERLAY_NAME``.
"""

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


class TestOverlayCodeDefaultTier(TestCase):
    @pytest.fixture(autouse=True)
    def _overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        monkeypatch.delenv("T3_REVIEW_SKILL", raising=False)
        self.monkeypatch = monkeypatch

    def test_code_default_wins_over_dataclass_default_with_no_db_row(self) -> None:
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().review_skill == "ac-reviewing-codebase"

    def test_db_global_row_overrides_the_code_default(self) -> None:
        ConfigSetting.objects.set_value("review_skill", "custom-review-skill")
        assert get_effective_settings().review_skill == "custom-review-skill"

    def test_db_overlay_row_overrides_the_code_default(self) -> None:
        ConfigSetting.objects.set_value("review_skill", "overlay-skill", scope="t3-teatree")
        assert get_effective_settings().review_skill == "overlay-skill"

    def test_env_overrides_the_code_default(self) -> None:
        self.monkeypatch.setenv("T3_REVIEW_SKILL", "env-skill")
        assert get_effective_settings().review_skill == "env-skill"

    def test_field_only_key_code_default_matches_public_constant(self) -> None:
        # The six field-only promotions relocate the constant to the overlay code
        # default without changing the effective value (default == dataclass default).
        settings = get_effective_settings()
        assert settings.scanning_news_skill == "scanning-news"
        assert settings.eval_local_skill == "eval"
        assert settings.backlog_sweep_skill == "sweeping-tickets"
        assert settings.dogfood_smoke_skill == "dogfood-smoke"
        assert settings.architectural_review_skill == "ac-reviewing-codebase"

    def test_field_only_key_db_row_still_overrides(self) -> None:
        ConfigSetting.objects.set_value("scanning_news_skill", "custom-news-skill")
        assert get_effective_settings().scanning_news_skill == "custom-news-skill"
