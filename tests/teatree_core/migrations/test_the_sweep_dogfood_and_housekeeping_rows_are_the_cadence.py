# test-path: cross-cutting
"""An operator's stored cadence lands on the Loop row before the key is deleted.

A data migration that deletes the rows and folds nothing looks identical to one that
worked — the shipped defaults already agree with the seeded rows — so each case is
paired with the value it must MOVE and the row it must leave alone. The rows are
written directly: they pre-date the retirement, so the admin write path would refuse
the keys today.
"""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from django.test import TestCase

from teatree.core.migrations._cadence_fold import DivergentScopedRowsError, fold_cadence, fold_cadence_or_default
from teatree.core.models import ConfigSetting, Loop

_MIGRATION = importlib.import_module(
    "teatree.core.migrations.0096_the_sweep_dogfood_and_housekeeping_rows_are_the_cadence"
)
_EVAL_AND_TRIAGE = importlib.import_module("teatree.core.migrations.0092_the_eval_and_triage_rows_are_the_cadence")
_EDITOR = SimpleNamespace(connection=connection)

_HOUR = 3600


def _fold() -> None:
    fold_cadence(_MIGRATION.FOLDS)(apps, _EDITOR)


def _stored(key: str, hours: int, scope: str = "") -> None:
    ConfigSetting.objects.create(scope=scope, key=key, value=hours)


def _loop(name: str, delay_seconds: int) -> None:
    Loop.objects.update_or_create(name=name, defaults={"delay_seconds": delay_seconds})


class TestAStoredCadenceMovesOntoItsRow(TestCase):
    def test_each_key_folds_onto_the_loop_it_gated(self) -> None:
        for loop_name, key in _MIGRATION.FOLDS:
            _loop(loop_name, 111)
            _stored(key, 6)

        _fold()

        for loop_name, key in _MIGRATION.FOLDS:
            assert Loop.objects.get(name=loop_name).delay_seconds == 6 * _HOUR
            assert not ConfigSetting.objects.filter(key=key).exists()

    def test_an_unset_key_leaves_the_seeded_row_alone(self) -> None:
        _loop("housekeeping", _HOUR)

        _fold()

        assert Loop.objects.get(name="housekeeping").delay_seconds == _HOUR

    def test_a_loop_no_key_names_is_untouched(self) -> None:
        _loop("eval_local", 604800)
        _stored("backlog_sweep_cadence_hours", 6)

        _fold()

        assert Loop.objects.get(name="eval_local").delay_seconds == 604800


class TestDisagreeingScopesAreRefused(TestCase):
    def test_two_scopes_with_different_hours_raise_rather_than_pick_one(self) -> None:
        _loop("dogfood", 24 * _HOUR)
        _stored("dogfood_smoke_cadence_hours", 6)
        _stored("dogfood_smoke_cadence_hours", 12, scope="t3-teatree")

        with pytest.raises(DivergentScopedRowsError):
            _fold()

        assert Loop.objects.get(name="dogfood").delay_seconds == 24 * _HOUR


class TestAnUnsetKeyFoldsItsRetiredDefault(TestCase):
    """A box that never stored the key ran on its retired default, not on the row."""

    def test_the_eval_and_triage_rows_take_the_weekly_and_daily_defaults(self) -> None:
        _loop("eval_local", 86400)
        _loop("triage_assessor", _HOUR)

        fold_cadence_or_default(_EVAL_AND_TRIAGE.FOLDS)(apps, _EDITOR)

        assert Loop.objects.get(name="eval_local").delay_seconds == 168 * _HOUR
        assert Loop.objects.get(name="triage_assessor").delay_seconds == 24 * _HOUR

    def test_a_stored_value_still_wins_over_the_default(self) -> None:
        _loop("eval_local", 86400)
        _stored("eval_local_cadence_hours", 12)

        fold_cadence_or_default(_EVAL_AND_TRIAGE.FOLDS)(apps, _EDITOR)

        assert Loop.objects.get(name="eval_local").delay_seconds == 12 * _HOUR
        assert not ConfigSetting.objects.filter(key="eval_local_cadence_hours").exists()
