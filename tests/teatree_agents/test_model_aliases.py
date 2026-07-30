"""The CLI family aliases (``opus``/``sonnet``/``haiku``) are DERIVED from the tier catalog."""

import pytest

from teatree.agents import model_tiering
from teatree.agents.model_aliases import family_alias_models
from teatree.agents.model_tiering import TIER_MODELS
from teatree.core.cost import tier_of_model


class TestFamilyAliasModels:
    """The CLI's ``opus``/``sonnet``/``haiku`` aliases are DERIVED from the catalog.

    A second hardcoded copy of the alias map is the desync the derivation exists to
    foreclose: a lane REQUESTING the previous generation while the transcript
    REPORTS the current one misfiles every dollar and reports a phantom fallback.
    """

    def test_every_tier_contributes_its_family_alias(self) -> None:
        assert family_alias_models() == {tier_of_model(model): model for model in TIER_MODELS.values()}

    def test_alias_follows_a_bumped_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The anti-vacuity assertion: a re-hardcoded map would keep returning the
        # shipped id here and this goes red.
        bumped = {**TIER_MODELS, "frontier": "claude-opus-9-9"}
        monkeypatch.setattr(model_tiering, "TIER_MODELS", bumped)
        assert family_alias_models()["opus"] == "claude-opus-9-9"

    def test_non_claude_pin_contributes_no_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # teatree never invents a family name for a swapped-in third-party id.
        monkeypatch.setattr(model_tiering, "TIER_MODELS", {**TIER_MODELS, "cheap": "vendor/some-model"})
        assert "haiku" not in family_alias_models()
