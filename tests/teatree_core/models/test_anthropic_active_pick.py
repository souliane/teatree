r"""The sticky routing pointer (``teatree.core.models.anthropic_active_pick``)."""

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import AnthropicActivePick
from teatree.core.models.anthropic_active_pick import AnthropicActivePickManager


class TestActivePickManager(TestCase):
    def test_objects_is_the_active_pick_manager(self) -> None:
        assert isinstance(AnthropicActivePick.objects, AnthropicActivePickManager)

    def test_manager_set_pick_and_pick_for_round_trip(self) -> None:
        manager = AnthropicActivePick.objects
        assert isinstance(manager, AnthropicActivePickManager)
        manager.set_pick("api_key", "ov", "anthropic/x/api")
        assert manager.pick_for("api_key", "ov") == "anthropic/x/api"


class TestActivePick(TestCase):
    def test_pick_for_is_none_until_set(self) -> None:
        assert AnthropicActivePick.objects.pick_for("oauth", "myoverlay") is None

    def test_set_pick_then_read_it_back(self) -> None:
        AnthropicActivePick.objects.set_pick("oauth", "myoverlay", "anthropic/a/oauth")
        assert AnthropicActivePick.objects.pick_for("oauth", "myoverlay") == "anthropic/a/oauth"

    def test_set_pick_upserts_the_single_row(self) -> None:
        AnthropicActivePick.objects.set_pick("oauth", "myoverlay", "anthropic/a/oauth")
        AnthropicActivePick.objects.set_pick("oauth", "myoverlay", "anthropic/b/oauth")
        rows = AnthropicActivePick.objects.filter(kind="oauth", scope="myoverlay")
        assert rows.count() == 1
        assert rows.get().pass_path == "anthropic/b/oauth"

    def test_kind_and_scope_are_independent_pointers(self) -> None:
        AnthropicActivePick.objects.set_pick("oauth", "overlay-a", "anthropic/a/oauth")
        AnthropicActivePick.objects.set_pick("oauth", "overlay-b", "anthropic/b/oauth")
        AnthropicActivePick.objects.set_pick("api_key", "overlay-a", "anthropic/a/api")
        assert AnthropicActivePick.objects.pick_for("oauth", "overlay-a") == "anthropic/a/oauth"
        assert AnthropicActivePick.objects.pick_for("oauth", "overlay-b") == "anthropic/b/oauth"
        assert AnthropicActivePick.objects.pick_for("api_key", "overlay-a") == "anthropic/a/api"

    def test_str_distinguishes_global_and_overlay_scopes(self) -> None:
        assert "global" in str(AnthropicActivePick(kind="oauth", scope="", pass_path="p"))
        assert "overlay:ov" in str(AnthropicActivePick(kind="oauth", scope="ov", pass_path="p"))

    def test_kind_scope_pair_is_unique(self) -> None:
        AnthropicActivePick.objects.create(kind="oauth", scope="x", pass_path="p1")
        with pytest.raises(IntegrityError), transaction.atomic():
            AnthropicActivePick.objects.create(kind="oauth", scope="x", pass_path="p2")


class TestUnpinAccount(TestCase):
    """A spent account is provably wrong for EVERY scope, so its pins all go at once."""

    def _pin_everywhere(self, pass_path: str) -> None:
        for scope in ("", "alpha", "beta"):
            AnthropicActivePick.objects.set_pick("oauth", scope, pass_path)

    def test_unpin_account_clears_every_scope_that_named_it(self) -> None:
        self._pin_everywhere("anthropic/a/oauth")
        AnthropicActivePick.objects.set_pick("api_key", "", "anthropic/e/api")

        removed = AnthropicActivePick.objects.unpin_account("anthropic/a/oauth")

        assert removed == 3
        assert not AnthropicActivePick.objects.filter(pass_path="anthropic/a/oauth").exists()
        assert AnthropicActivePick.objects.pick_for("api_key", "") == "anthropic/e/api"

    def test_unpin_account_leaves_other_accounts_pins_alone(self) -> None:
        AnthropicActivePick.objects.set_pick("oauth", "alpha", "anthropic/a/oauth")
        AnthropicActivePick.objects.set_pick("oauth", "beta", "anthropic/b/oauth")

        assert AnthropicActivePick.objects.unpin_account("anthropic/a/oauth") == 1
        assert AnthropicActivePick.objects.pick_for("oauth", "beta") == "anthropic/b/oauth"

    def test_unpin_account_is_idempotent(self) -> None:
        self._pin_everywhere("anthropic/a/oauth")
        AnthropicActivePick.objects.unpin_account("anthropic/a/oauth")

        assert AnthropicActivePick.objects.unpin_account("anthropic/a/oauth") == 0
