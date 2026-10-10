"""Read-only recipe score and approval command tests."""

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from teatree.core.factory.factory_recipe import recipe_sha
from teatree.core.factory.factory_score import FactoryScoreDict, ScoredSignalDict
from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot


def _score(*args: str) -> str:
    """The human view — the seam routes it to stderr; stdout is the JSON channel."""
    err = StringIO()
    call_command("recipe", "score", *args, stderr=err)
    return err.getvalue()


def _score_json(*args: str) -> str:
    out = StringIO()
    call_command("recipe", "score", "--json", *args, stdout=out)
    return out.getvalue()


class TestRecipeScoreReadOnly(TestCase):
    def test_score_computes_read_only(self) -> None:
        output = _score()
        assert "factory score" in output
        assert FactoryScoreSnapshot.objects.count() == 0

    def test_json_output_carries_the_payload_shape(self) -> None:
        payload = json.loads(_score_json())
        for key in ("aggregate", "verdict", "coverage", "recipe_sha", "recipe_approved", "signals"):
            assert key in payload

    def test_record_writes_one_snapshot_and_one_unapproved_recipe_question(self) -> None:
        _score("--record")
        assert FactoryScoreSnapshot.objects.count() == 1
        assert DeferredQuestion.objects.count() == 1
        row = DeferredQuestion.objects.get()
        assert row.evidence["decision"] == "product_scope"
        assert row.question == "Can the factory use the new scoring recipe?"
        assert recipe_sha()[:12] not in row.question + str(row.evidence)
        _score("--record")
        assert FactoryScoreSnapshot.objects.count() == 2
        assert DeferredQuestion.objects.count() == 1


class TestApprove(TestCase):
    def test_pins_recipe_sha_into_config_setting(self) -> None:
        out = StringIO()
        call_command("recipe", "approve", stdout=out)
        stored = ConfigSetting.objects.get_effective("approved_recipe_sha", scope="")
        assert stored == recipe_sha()
        assert recipe_sha()[:12] in out.getvalue()


class TestScorePayloadShape(TestCase):
    def test_json_matches_the_declared_wire_shapes(self) -> None:
        payload = json.loads(_score_json())
        assert set(payload) == set(FactoryScoreDict.__annotations__)
        for row in payload["signals"]:
            assert set(row) == set(ScoredSignalDict.__annotations__)
