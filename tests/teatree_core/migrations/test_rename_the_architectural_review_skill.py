"""The ``0119`` data migration moves stored rows from ``ac-reviewing-codebase`` to ``architectural-review``."""

from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from teatree.core.models import ConfigSetting, Loop, Prompt

_migration = import_module("teatree.core.migrations.0119_rename_the_architectural_review_skill")
forward = _migration.forward
backward = _migration.backward
_SCHEMA_EDITOR = SimpleNamespace(connection=connection)

_OLD_DESCRIPTION = "Dispatches a sub-agent every 3h to run a review via the ac-reviewing-codebase skill."
_NEW_DESCRIPTION = "Dispatches a sub-agent every 3h to run a review via the architectural-review skill."


class TestRenameTheArchitecturalReviewSkill(TestCase):
    def _arch_review(self, *, body: str, description: str) -> Prompt:
        prompt = Prompt.objects.get(name="arch_review")
        Prompt.objects.filter(pk=prompt.pk).update(body=body, description=description)
        Loop.objects.filter(name="arch_review").update(description=description)
        return prompt

    def test_the_seeded_prompt_is_rewritten_and_its_old_body_kept_as_a_version(self) -> None:
        prompt = self._arch_review(body=_migration.OLD_PROMPT_BODY, description=_OLD_DESCRIPTION)
        versions_before = prompt.versions.count()

        forward(apps, _SCHEMA_EDITOR)

        prompt.refresh_from_db()
        assert prompt.body == _migration.NEW_PROMPT_BODY
        assert prompt.description == _NEW_DESCRIPTION
        assert Loop.objects.get(name="arch_review").description == _NEW_DESCRIPTION
        assert prompt.versions.count() == versions_before + 1
        assert prompt.versions.order_by("-version").values_list("body", flat=True).first() == _migration.OLD_PROMPT_BODY

    def test_an_owner_edited_prompt_body_is_left_alone(self) -> None:
        prompt = self._arch_review(body="Review with ac-reviewing-codebase, my way.", description="")
        versions_before = prompt.versions.count()

        forward(apps, _SCHEMA_EDITOR)

        prompt.refresh_from_db()
        assert prompt.body == "Review with ac-reviewing-codebase, my way."
        assert prompt.versions.count() == versions_before

    def test_stored_skill_settings_move_and_other_values_stay(self) -> None:
        ConfigSetting.objects.create(key="architectural_review_skill", value="ac-reviewing-codebase")
        ConfigSetting.objects.create(
            scope="acme", key="review_skill", value="t3:ac-reviewing-codebase", seed_value="t3:ac-reviewing-codebase"
        )
        ConfigSetting.objects.create(scope="other", key="review_skill", value="custom-review")

        forward(apps, _SCHEMA_EDITOR)

        assert {(row.scope, row.key): (row.value, row.seed_value) for row in ConfigSetting.objects.all()} == {
            ("", "architectural_review_skill"): ("architectural-review", None),
            ("acme", "review_skill"): ("t3:architectural-review", "t3:architectural-review"),
            ("other", "review_skill"): ("custom-review", None),
        }

    def test_backward_restores_every_rewritten_row(self) -> None:
        prompt = self._arch_review(body=_migration.OLD_PROMPT_BODY, description=_OLD_DESCRIPTION)
        ConfigSetting.objects.create(key="review_skill", value="ac-reviewing-codebase")

        forward(apps, _SCHEMA_EDITOR)
        backward(apps, _SCHEMA_EDITOR)

        prompt.refresh_from_db()
        assert prompt.body == _migration.OLD_PROMPT_BODY
        assert prompt.description == _OLD_DESCRIPTION
        assert Loop.objects.get(name="arch_review").description == _OLD_DESCRIPTION
        assert ConfigSetting.objects.get(key="review_skill").value == "ac-reviewing-codebase"
