# test-path: cross-cutting
"""A retired mechanism must lose its STORAGE, not just its prose (#3921).

The audit's sharpest finding was not that mechanisms accumulate — it was *how*
they are retired. Five guards on the merge-decision path had been retired in
docstring and behaviour while the table, the column, the enum member and the
dispatch code path all stayed. That shape is worse than never retiring: a dead
mechanism that still takes locks, still holds rows and still reads as a live
state in the merge gate is one an operator cannot tell from a live one, and the
next reader wires it back up because the storage says it exists.

So this is the completeness half of a retirement. The ledger below names the
storage each retirement had to reach; the assertions drive Django's own app
registry, so a re-added model, field or enum member reds here rather than
surviving as an orphan behind prose that says it is gone.

The direction contract of ``test_ratchet_direction`` holds here too: every
assertion is an ABSENCE. Retiring more can never turn this red, so it taxes no
improvement — only a silent re-introduction.
"""

from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import models

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Models whose retirement dropped the table (#3921 step 2).
RETIRED_MODELS: tuple[str, ...] = ("ReviewLoop", "ReviewLoopRound")

#: ``(model, field)`` pairs whose retirement dropped the column.
RETIRED_FIELDS: tuple[tuple[str, str], ...] = (("ReviewRequestPost", "last_nag_step"),)

#: ``(model, field, value)`` triples whose retirement dropped the enum member.
RETIRED_CHOICE_VALUES: tuple[tuple[str, str, str], ...] = (
    ("MRReviewLock", "state", "verdict_pending"),
    ("ReviewAssignment", "state", "eyes_added"),
)


def _field(model_name: str, field_name: str) -> models.Field | None:
    try:
        return apps.get_model("core", model_name)._meta.get_field(field_name)
    except FieldDoesNotExist:
        return None


def _offered_values(field: models.Field) -> set[str]:
    # ``Field.choices`` accepts a mapping / callable / pair-iterable, but ``Field.__init__``
    # normalizes every one of them to the pair list before the instance is reachable here.
    pairs = cast("Iterable[tuple[object, object]]", field.choices or ())
    return {str(value) for value, _label in pairs}


class TestRetiredModelsLostTheirTable:
    def test_no_retired_model_is_still_registered(self) -> None:
        surviving = [name for name in RETIRED_MODELS if apps.all_models["core"].get(name.lower()) is not None]
        assert surviving == [], f"retired model(s) still registered: {surviving}"


class TestRetiredFieldsLostTheirColumn:
    def test_no_retired_field_is_still_declared(self) -> None:
        surviving = [f"{model}.{field}" for model, field in RETIRED_FIELDS if _field(model, field) is not None]
        assert surviving == [], f"retired field(s) still declared: {surviving}"


class TestRetiredChoiceValuesLostTheirEnumMember:
    def test_no_retired_choice_value_is_still_offered(self) -> None:
        surviving = []
        for model_name, field_name, value in RETIRED_CHOICE_VALUES:
            field = _field(model_name, field_name)
            if field is not None and value in _offered_values(field):
                surviving.append(f"{model_name}.{field_name}={value}")
        assert surviving == [], f"retired enum member(s) still offered: {surviving}"
