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
from teatree.core.gates.single_branch_repo_guard import find_second_branch_creation, resolve_pinned_branch
from teatree.core.models import ConfigSetting
from teatree.core.overlay_loader import get_overlay


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


class TestSingleBranchReposReachesTheGate(TestCase):
    """The declaration in ``overlay_settings.py`` is what the gate reads (#3-audit).

    ``single_branch_repos`` was declared on the overlay and resolved ``[]``, so the
    gate that reads it through ``get_effective_settings`` was inert while the rule
    it enforces was being violated. The promotion closes that: the overlay's
    declaration IS the effective value, with no second DB source of truth.
    """

    @pytest.fixture(autouse=True)
    def _overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        self.monkeypatch = monkeypatch

    def _declare(self, entries: list[str]) -> None:
        self.monkeypatch.setattr(get_overlay("t3-teatree").config, "single_branch_repos", entries)

    def test_overlay_declaration_is_the_effective_value(self) -> None:
        assert ConfigSetting.objects.count() == 0
        self._declare(["group/widget-core=chore/fork-bootstrap"])
        assert get_effective_settings().single_branch_repos == ["group/widget-core=chore/fork-bootstrap"]

    def test_declaring_nothing_leaves_the_gate_inert(self) -> None:
        assert get_effective_settings().single_branch_repos == []

    def test_db_row_still_overrides_the_declaration(self) -> None:
        self._declare(["group/widget-core=chore/fork-bootstrap"])
        ConfigSetting.objects.set_value("single_branch_repos", ["group/other=main"], scope="t3-teatree")
        assert get_effective_settings().single_branch_repos == ["group/other=main"]

    def test_a_declared_repo_refuses_a_second_branch_end_to_end(self) -> None:
        self._declare(["group/widget-core=chore/fork-bootstrap"])
        entries = get_effective_settings().single_branch_repos
        pinned = resolve_pinned_branch("git@example.com:group/widget-core.git", entries)
        assert pinned == "chore/fork-bootstrap"
        assert find_second_branch_creation("git checkout -b feature/x", pinned_branch=pinned) is not None

    def test_an_undeclared_repo_is_untouched_end_to_end(self) -> None:
        self._declare(["group/widget-core=chore/fork-bootstrap"])
        entries = get_effective_settings().single_branch_repos
        pinned = resolve_pinned_branch("git@example.com:group/unrelated.git", entries)
        assert pinned == ""
        assert find_second_branch_creation("git checkout -b feature/x", pinned_branch=pinned) is None
