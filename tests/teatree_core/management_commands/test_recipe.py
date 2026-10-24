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
from teatree.loop.scanners.owner_actions import OwnerActionScanner
from tests._owner_channel import tap_on_slack


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

    def test_the_approve_button_on_the_recipe_question_can_be_tapped(self) -> None:
        _score("--record")
        row = DeferredQuestion.objects.get()

        assert tap_on_slack(row, "Approve")

        row.refresh_from_db()
        assert (row.answer_text, row.resolved_via) == ("Approve", "slack")

    def test_the_approve_answer_pins_the_recipe_before_application_is_stamped(self) -> None:
        _score("--record")
        row = DeferredQuestion.objects.get()
        assert tap_on_slack(row, "Approve")

        OwnerActionScanner().scan()

        row.refresh_from_db()
        assert ConfigSetting.objects.get_effective("approved_recipe_sha", scope="") == recipe_sha()
        assert row.applied_at is not None

    def test_the_keep_answer_is_applied_without_approving_the_recipe(self) -> None:
        _score("--record")
        row = DeferredQuestion.objects.get()
        assert tap_on_slack(row, "Keep the current recipe")

        OwnerActionScanner().scan()

        row.refresh_from_db()
        assert ConfigSetting.objects.get_effective("approved_recipe_sha", scope="") is None
        assert row.applied_at is not None

    def test_an_existing_markerless_recipe_answer_still_applies(self) -> None:
        _score("--record")
        row = DeferredQuestion.objects.get()
        DeferredQuestion.objects.filter(pk=row.pk).update(dedupe_marker="")
        row.refresh_from_db()
        assert tap_on_slack(row, "Approve")

        OwnerActionScanner().scan()

        row.refresh_from_db()
        assert ConfigSetting.objects.get_effective("approved_recipe_sha", scope="") == recipe_sha()
        assert row.applied_at is not None

    def test_a_changed_recipe_is_not_stamped_as_applied(self) -> None:
        _score("--record")
        row = DeferredQuestion.objects.get()
        assert tap_on_slack(row, "Approve")
        DeferredQuestion.objects.filter(pk=row.pk).update(dedupe_marker="recipe:changed")

        OwnerActionScanner().scan()

        row.refresh_from_db()
        assert ConfigSetting.objects.get_effective("approved_recipe_sha", scope="") is None
        assert row.applied_at is None


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
