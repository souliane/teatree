"""``t3 <overlay> recipe score|approve`` command tests (SIG-PR-2).

Pins the flag-gated OFF contract: with ``factory_score_enabled`` off (the shipped
state) ``score`` still COMPUTES read-only, but ``--record`` refuses and writes
NOTHING — zero snapshot rows, zero deferred questions. Flag on, a scored read
persists exactly one snapshot and queues exactly one deduped approval question per
unapproved sha; ``approve`` pins the sha so ``recipe_approved`` flips true.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.factory_recipe import recipe_sha
from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot


def _score(*args: str) -> str:
    out = StringIO()
    call_command("recipe", "score", *args, stdout=out)
    return out.getvalue()


class TestFlagOff(TestCase):
    def test_record_refuses_and_writes_nothing(self) -> None:
        with pytest.raises(SystemExit):
            _score("--record")
        assert FactoryScoreSnapshot.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0

    def test_score_computes_read_only(self) -> None:
        output = _score()
        assert "factory score" in output
        assert FactoryScoreSnapshot.objects.count() == 0

    def test_json_output_carries_the_payload_shape(self) -> None:
        payload = json.loads(_score("--json"))
        for key in ("aggregate", "verdict", "coverage", "recipe_sha", "recipe_approved", "signals"):
            assert key in payload


class TestFlagOn(TestCase):
    def setUp(self) -> None:
        call_command("config_setting", "set", "factory_score_enabled", "true")

    def test_record_persists_one_snapshot(self) -> None:
        _score("--record")
        assert FactoryScoreSnapshot.objects.count() == 1
        snap = FactoryScoreSnapshot.objects.get()
        assert snap.recipe_sha == recipe_sha()

    def test_unapproved_recipe_queues_exactly_one_deduped_question(self) -> None:
        _score("--record")
        _score("--record")
        # Two scored reads against the same unapproved sha → still ONE question.
        assert DeferredQuestion.objects.count() == 1
        assert FactoryScoreSnapshot.objects.count() == 2

    def test_approved_recipe_records_without_a_question(self) -> None:
        call_command("recipe", "approve")
        _score("--record")
        assert DeferredQuestion.objects.count() == 0
        assert FactoryScoreSnapshot.objects.get().recipe_approved is True


class TestApprove(TestCase):
    def test_pins_recipe_sha_into_config_setting(self) -> None:
        out = StringIO()
        call_command("recipe", "approve", stdout=out)
        stored = ConfigSetting.objects.get_effective("approved_recipe_sha", scope="")
        assert stored == recipe_sha()
        assert recipe_sha()[:12] in out.getvalue()
