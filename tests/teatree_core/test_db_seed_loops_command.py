"""``t3 <overlay> db seed-loops`` — the reachable route to the shipped loop rows.

The modes and schedules a box actually runs on are created by ``seed_loops``, which
``t3 setup`` invokes as its LAST step. Every earlier step of setup is therefore a way
to lose them, and a box that loses them holds only the ``offline`` mode its initial
migration creates: holiday mode, every loop off, silently, forever.

``t3 doctor`` tells the operator to recover by running ``python -m teatree
seed_loops``. That instruction is unrunnable in a container whose only teatree
entry point is the ``t3`` console script — the interpreter carrying teatree is
inside a ``uv`` tool env nothing puts on ``PATH``. This verb is the one an operator
(and a deploy entrypoint) can actually reach.
"""

from io import StringIO
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import Mode, ModeSchedule


class DbSeedLoopsCommandTest(django.test.TestCase):
    def setUp(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()

    def test_seeds_the_shipped_modes_and_schedules(self) -> None:
        call_command("db", "seed-loops", stdout=StringIO())

        assert Mode.objects.exists()
        assert ModeSchedule.objects.exists()

    def test_re_running_creates_nothing_new(self) -> None:
        call_command("db", "seed-loops", stdout=StringIO())
        before = (Mode.objects.count(), ModeSchedule.objects.count())

        call_command("db", "seed-loops", stdout=StringIO())

        assert (Mode.objects.count(), ModeSchedule.objects.count()) == before

    def test_re_running_leaves_an_edited_row_untouched(self) -> None:
        call_command("db", "seed-loops", stdout=StringIO())
        edited = Mode.objects.first()
        assert edited is not None
        edited.description = "the operator's own wording"
        edited.save(update_fields=["description"])

        call_command("db", "seed-loops", stdout=StringIO())

        edited.refresh_from_db()
        assert edited.description == "the operator's own wording"

    def test_reports_what_it_created(self) -> None:
        out = StringIO()
        call_command("db", "seed-loops", stdout=out)

        assert "created" in out.getvalue()

    def test_fails_closed_when_the_seed_errors(self) -> None:
        # A swallowed error is the whole defect: the deploy would report a converged
        # box that then sits in holiday mode.
        with (
            patch(
                "teatree.core.management.commands.db.call_command",
                side_effect=RuntimeError("seed exploded"),
            ),
            pytest.raises(RuntimeError),
        ):
            call_command("db", "seed-loops", stdout=StringIO(), stderr=StringIO())
