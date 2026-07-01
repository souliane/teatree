r"""The sticky routing pointer (``teatree.core.models.anthropic_active_pick``)."""

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import AnthropicActivePick


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

    def test_kind_scope_pair_is_unique(self) -> None:
        AnthropicActivePick.objects.create(kind="oauth", scope="x", pass_path="p1")
        with pytest.raises(IntegrityError), transaction.atomic():
            AnthropicActivePick.objects.create(kind="oauth", scope="x", pass_path="p2")
