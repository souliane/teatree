"""A ``Loop`` row disagreeing with the shipped ``[loops.<name>]`` table is visible and fixable.

The live ``review`` row said ``colleague_facing=1`` while the shipped table said
``false``, so the away-class admission gate skipped the loop — cold review stopped,
starving the ``merge_safe`` verdict ``pr_sweep`` merges on, with no signal anywhere.
``colleague_facing`` is admin-editable, so the seed still preserves an operator's
choice; the disagreement is REPORTED, and written back only on an explicit reconcile.
"""

import contextlib
import io
from unittest import mock

import django.test
from django.core.management import call_command

from teatree.cli.doctor.app import _check_loop_classification_drift
from teatree.core.models import Loop
from teatree.loops.seed import DEFAULT_LOOPS, LoopSeedSpec, seed_default_loops_and_prompts
from teatree.loops.seed_drift import classification_drift, reconcile_classification


def _spec(name: str) -> LoopSeedSpec:
    return next(spec for spec in DEFAULT_LOOPS if spec.name == name)


class TestClassificationDriftIsDetectable(django.test.TestCase):
    def test_drift_names_the_loop_and_both_values(self) -> None:
        assert _spec("review").colleague_facing is False
        Loop.objects.filter(name="review").update(colleague_facing=True)

        findings = classification_drift()

        assert len(findings) == 1
        assert "review" in findings[0]
        assert "colleague_facing=True" in findings[0]
        assert "False" in findings[0]

    def test_an_agreeing_table_reports_nothing(self) -> None:
        seed_default_loops_and_prompts()

        assert classification_drift() == []

    def test_a_loop_absent_from_the_shipped_table_is_not_drift(self) -> None:
        Loop.objects.create(
            name="operator-custom",
            script="src/teatree/loops/operator_custom/loop.py",
            delay_seconds=300,
            colleague_facing=True,
        )

        assert classification_drift() == []


class TestExplicitReconcile(django.test.TestCase):
    def test_reconcile_writes_the_shipped_value_back(self) -> None:
        Loop.objects.filter(name="review").update(colleague_facing=True)

        changed = reconcile_classification()

        assert any("review" in line for line in changed)
        assert Loop.objects.get(name="review").colleague_facing is False
        assert classification_drift() == []

    def test_reconcile_is_idempotent(self) -> None:
        reconcile_classification()

        assert reconcile_classification() == []

    def test_reconcile_leaves_operator_tunable_fields_alone(self) -> None:
        Loop.objects.filter(name="review").update(colleague_facing=True, enabled=True, delay_seconds=99)

        reconcile_classification()

        row = Loop.objects.get(name="review")
        assert row.enabled is True
        assert row.delay_seconds == 99

    def test_seed_alone_never_reconciles(self) -> None:
        Loop.objects.filter(name="review").update(colleague_facing=True)

        seed_default_loops_and_prompts()

        assert Loop.objects.get(name="review").colleague_facing is True


class TestSeedLoopsCommandFlag(django.test.TestCase):
    def test_flag_reconciles_and_reports(self) -> None:
        Loop.objects.filter(name="review").update(colleague_facing=True)
        out = io.StringIO()

        call_command("seed_loops", "--reconcile-classification", stdout=out)

        assert "review" in out.getvalue()
        assert Loop.objects.get(name="review").colleague_facing is False

    def test_bare_command_reports_the_drift_without_writing(self) -> None:
        Loop.objects.filter(name="review").update(colleague_facing=True)
        out = io.StringIO()

        call_command("seed_loops", stdout=out)

        assert "--reconcile-classification" in out.getvalue()
        assert Loop.objects.get(name="review").colleague_facing is True


class TestDoctorSurfacesTheDrift(django.test.TestCase):
    def test_doctor_check_names_the_drifting_loop_and_the_fix(self) -> None:
        Loop.objects.filter(name="review").update(colleague_facing=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = _check_loop_classification_drift()

        assert ok is False
        assert "review" in out.getvalue()
        assert "--reconcile-classification" in out.getvalue()

    def test_doctor_check_is_quiet_when_the_stores_agree(self) -> None:
        reconcile_classification()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = _check_loop_classification_drift()

        assert ok is True
        assert out.getvalue() == ""


class TestPartialTables(django.test.TestCase):
    def test_a_shipped_loop_with_no_row_yet_is_not_drift(self) -> None:
        Loop.objects.filter(name="review").delete()

        assert classification_drift() == []

    def test_a_raising_read_degrades_the_doctor_check_to_ok(self) -> None:
        out = io.StringIO()
        with (
            mock.patch("teatree.loops.seed_drift.classification_drift", side_effect=RuntimeError("db down")),
            contextlib.redirect_stdout(out),
        ):
            ok = _check_loop_classification_drift()

        assert ok is True
        assert "crashed" in out.getvalue()
