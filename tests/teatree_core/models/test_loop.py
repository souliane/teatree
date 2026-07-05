"""DB-configured autonomous loop model (#1796).

Each loop is its own autonomous row with its own cadence — a fixed
``delay_seconds`` interval or a ``daily_at`` once-per-day wall-clock time. These
tests pin the cadence gate (interval + daily), the manager surface, and the
one-time seed of the autonomous loop set. Integration-first against the real DB;
``demo-*`` names never collide with the seeded production loop names.
"""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.core.models import Loop, Prompt


def _prompt(name: str = "demo-prompt", body: str = "do x") -> Prompt:
    """A reusable :class:`Prompt` row for loops under test (FK target, #2513).

    Idempotent by name so several loops in one test can share one prompt (the FK
    is many-loops→one-prompt) without tripping the unique-name constraint.
    """
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": body})
    return prompt


class TestLoopDefaults(TestCase):
    def test_enabled_default_last_run_absent(self) -> None:
        loop = Loop.objects.create(name="demo-x", delay_seconds=300, prompt=_prompt("p-x"))
        assert loop.enabled is True
        assert loop.last_run_at is None
        assert loop.daily_at is None

    def test_str_describes_name_state_and_cadence(self) -> None:
        loop = Loop.objects.create(name="demo-ship", delay_seconds=300, prompt=_prompt("p-ship"))
        rendered = str(loop)
        assert "demo-ship" in rendered
        assert "enabled" in rendered
        assert "every 300s" in rendered


class TestLoopAdditiveFields(TestCase):
    """Phase 0 additive fields default to empty/true with zero behaviour change."""

    def test_script_run_in_sub_agent_description_overlay_defaults(self) -> None:
        loop = Loop.objects.create(name="demo-add", delay_seconds=300, prompt=_prompt("p-add"))
        assert loop.script == ""
        assert loop.run_in_sub_agent is True
        assert loop.description == ""
        assert loop.overlay == ""

    def test_script_only_loop_round_trips(self) -> None:
        Loop.objects.create(name="demo-script", delay_seconds=60, prompt=None, script="src/teatree/loops/demo/loop.py")
        reloaded = Loop.objects.get(name="demo-script")
        assert reloaded.script == "src/teatree/loops/demo/loop.py"
        assert reloaded.prompt_id is None

    def test_overlay_stores_backend_name_generically(self) -> None:
        Loop.objects.create(name="demo-overlay", delay_seconds=60, prompt=_prompt("p-ov"), overlay="some-backend")
        assert Loop.objects.get(name="demo-overlay").overlay == "some-backend"

    def test_run_in_sub_agent_can_be_disabled(self) -> None:
        Loop.objects.create(name="demo-inline", delay_seconds=60, prompt=_prompt("p-in"), run_in_sub_agent=False)
        assert Loop.objects.get(name="demo-inline").run_in_sub_agent is False

    def test_colleague_facing_defaults_false(self) -> None:
        loop = Loop.objects.create(name="demo-colleague-default", delay_seconds=300, prompt=_prompt("p-cf-default"))
        assert loop.colleague_facing is False

    def test_colleague_facing_can_be_set_true(self) -> None:
        Loop.objects.create(
            name="demo-colleague-true", delay_seconds=300, prompt=_prompt("p-cf-true"), colleague_facing=True
        )
        assert Loop.objects.get(name="demo-colleague-true").colleague_facing is True


class TestLoopNullableDelay(TestCase):
    """``delay_seconds`` is nullable for prompt loops that run every tick."""

    def test_delay_seconds_may_be_null(self) -> None:
        Loop.objects.create(name="demo-null", prompt=_prompt("p-null"), delay_seconds=None)
        assert Loop.objects.get(name="demo-null").delay_seconds is None

    def test_is_due_true_when_no_cadence_at_all(self) -> None:
        loop = Loop.objects.create(name="demo-no-cadence", prompt=_prompt("p-nc1"), delay_seconds=None)
        assert loop.is_due(timezone.now()) is True

    def test_cadence_label_handles_null_delay(self) -> None:
        loop = Loop.objects.create(name="demo-no-cadence", prompt=_prompt("p-nc2"), delay_seconds=None)
        assert loop.cadence_label == "every tick"

    def test_next_run_at_handles_null_delay(self) -> None:
        loop = Loop.objects.create(name="demo-no-cadence", prompt=_prompt("p-nc3"), delay_seconds=None)
        assert loop.next_run_at() is None


class TestLoopPromptScriptXor(TestCase):
    """Exactly one of ``prompt`` (FK) / ``script`` is set, enforced at clean() and in the DB."""

    def test_clean_rejects_both_prompt_and_script(self) -> None:
        loop = Loop(name="demo-both", delay_seconds=60, prompt=_prompt("p-both"), script="run.py")
        with pytest.raises(ValidationError):
            loop.full_clean()

    def test_clean_rejects_neither_prompt_nor_script(self) -> None:
        loop = Loop(name="demo-neither", delay_seconds=60, prompt=None, script="")
        with pytest.raises(ValidationError):
            loop.full_clean()

    def test_clean_rejects_script_with_null_delay(self) -> None:
        loop = Loop(name="demo-script-no-delay", delay_seconds=None, prompt=None, script="run.py")
        with pytest.raises(ValidationError):
            loop.full_clean()

    def test_clean_accepts_prompt_only(self) -> None:
        loop = Loop(name="demo-prompt-only", delay_seconds=60, prompt=_prompt("p-only"), script="")
        loop.full_clean()

    def test_clean_accepts_script_only_with_delay(self) -> None:
        loop = Loop(name="demo-script-only", delay_seconds=60, prompt=None, script="run.py")
        loop.full_clean()

    def test_db_constraint_rejects_both(self) -> None:
        prompt = _prompt("p-both-db")
        with pytest.raises(IntegrityError), transaction.atomic():
            Loop.objects.create(name="demo-both-db", delay_seconds=60, prompt=prompt, script="run.py")

    def test_db_constraint_rejects_neither(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Loop.objects.create(name="demo-neither-db", delay_seconds=60, prompt=None, script="")

    def test_db_constraint_rejects_script_without_delay(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Loop.objects.create(name="demo-script-no-delay-db", delay_seconds=None, prompt=None, script="run.py")


class TestLoopIntervalCadence(TestCase):
    def test_never_run_loop_is_due_no_age_no_next(self) -> None:
        now = timezone.now()
        loop = Loop.objects.create(name="demo-new", delay_seconds=300, prompt=_prompt())
        assert loop.seconds_since_run(now) is None
        assert loop.is_due(now) is True
        assert loop.next_run_at() is None
        assert loop.cadence_label == "every 300s"

    def test_recently_run_not_due_until_delay_elapses(self) -> None:
        now = timezone.now()
        loop = Loop.objects.create(
            name="demo-fresh", delay_seconds=300, prompt=_prompt(), last_run_at=now - dt.timedelta(seconds=120)
        )
        assert loop.is_due(now) is False
        loop.last_run_at = now - dt.timedelta(seconds=301)
        assert loop.is_due(now) is True

    def test_next_run_at_is_last_plus_delay(self) -> None:
        now = timezone.now()
        loop = Loop.objects.create(name="demo-next", delay_seconds=300, prompt=_prompt(), last_run_at=now)
        assert loop.next_run_at() == now + dt.timedelta(seconds=300)


@override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopDailyCadence(TestCase):
    def _at(self, hour: int, minute: int = 0) -> dt.datetime:
        return dt.datetime(2026, 6, 16, hour, minute, tzinfo=dt.UTC)

    def test_cadence_label_shows_daily_time(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, prompt=_prompt(), daily_at=dt.time(8, 0))
        assert loop.cadence_label == "daily 08:00"

    def test_never_run_not_due_before_scheduled_time(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, prompt=_prompt(), daily_at=dt.time(8, 0))
        assert loop.is_due(self._at(7)) is False

    def test_never_run_due_after_scheduled_time(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, prompt=_prompt(), daily_at=dt.time(8, 0))
        assert loop.is_due(self._at(9)) is True

    def test_not_due_again_after_running_today(self) -> None:
        loop = Loop.objects.create(
            name="demo-daily", delay_seconds=86400, prompt=_prompt(), daily_at=dt.time(8, 0), last_run_at=self._at(8, 1)
        )
        assert loop.is_due(self._at(9)) is False

    def test_due_next_day_after_scheduled_time(self) -> None:
        loop = Loop.objects.create(
            name="demo-daily", delay_seconds=86400, prompt=_prompt(), daily_at=dt.time(8, 0), last_run_at=self._at(8, 1)
        )
        tomorrow_9 = (self._at(8, 1) + dt.timedelta(days=1)).replace(hour=9, minute=0)
        assert loop.is_due(tomorrow_9) is True

    def test_next_run_at_returns_a_datetime(self) -> None:
        loop = Loop.objects.create(name="demo-daily", delay_seconds=86400, prompt=_prompt(), daily_at=dt.time(8, 0))
        assert loop.next_run_at() is not None


class TestLoopManager(TestCase):
    def test_enabled_excludes_disabled(self) -> None:
        Loop.objects.create(name="demo-on", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="demo-disabled", delay_seconds=60, prompt=_prompt(), enabled=False)
        names = {row.name for row in Loop.objects.enabled()}
        assert "demo-on" in names
        assert "demo-disabled" not in names

    def test_due_returns_enabled_overdue_only(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="demo-due", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="demo-cooling", delay_seconds=60, prompt=_prompt(), last_run_at=now)
        Loop.objects.create(name="demo-due-off", delay_seconds=60, prompt=_prompt(), enabled=False)
        due = {row.name for row in Loop.objects.due(now)}
        assert "demo-due" in due
        assert "demo-cooling" not in due
        assert "demo-due-off" not in due

    def test_mark_run_sets_last_run_at(self) -> None:
        Loop.objects.create(name="demo-mark", delay_seconds=60, prompt=_prompt())
        ts = timezone.now()
        Loop.objects.mark_run("demo-mark", ts)
        assert Loop.objects.get(name="demo-mark").last_run_at == ts

    def test_set_enabled_flips_the_row_toggle(self) -> None:
        Loop.objects.create(name="demo-toggle", delay_seconds=60, prompt=_prompt(), enabled=False)
        updated = Loop.objects.set_enabled("demo-toggle", enabled=True)
        assert updated == 1
        assert Loop.objects.get(name="demo-toggle").enabled is True
        Loop.objects.set_enabled("demo-toggle", enabled=False)
        assert Loop.objects.get(name="demo-toggle").enabled is False

    def test_set_enabled_is_a_no_op_for_an_absent_row(self) -> None:
        assert Loop.objects.set_enabled("demo-absent", enabled=True) == 0


class TestLoopSeed(TestCase):
    """The ``0001_initial`` migration seed lands one autonomous row per loop (#1796).

    Since the #2652 squash the default-loops seed is folded into ``0001_initial``
    as a ``RunPython`` (in its final per-loop-script / paused shape), so a fresh
    migrate lands exactly these rows.
    """

    def test_interval_loops_seeded_with_their_cadence(self) -> None:
        assert Loop.objects.get(name="inbox").delay_seconds == 60
        assert Loop.objects.get(name="audit").delay_seconds == 1800
        assert Loop.objects.get(name="followup").delay_seconds == 1800
        assert Loop.objects.get(name="arch_review").delay_seconds == 10800

    def test_orphan_slack_answer_row_is_not_seeded(self) -> None:
        # #2584: ``slack_answer`` has no registry MiniLoop, so the autonomous
        # fan-out can never run it — the seed never creates a ``slack_answer``
        # Loop row. It runs only via its dedicated ``loop-slack-answer`` ``/loop`` slot.
        assert not Loop.objects.filter(name="slack_answer").exists()

    def test_daily_loops_seeded_with_schedule(self) -> None:
        assert Loop.objects.get(name="news").daily_at == dt.time(8, 0)
        assert Loop.objects.get(name="dream").daily_at == dt.time(3, 0)
        assert Loop.objects.get(name="dogfood").delay_seconds == 86400

    def test_eval_local_seeded_paused_daily(self) -> None:
        # The #2513 cutover seeds every loop PAUSED: the seed lands each row
        # ``enabled=False`` directly — the row IS seeded with its daily cadence,
        # just not enabled until an operator turns it on.
        loop = Loop.objects.get(name="eval_local")
        assert loop.enabled is False
        assert loop.delay_seconds == 86400

    def test_every_loop_is_its_own_autonomous_row(self) -> None:
        # 22 default loops (#2584, +1 snapshot_warmer, +2 for #22 issue_disposition +
        # backlog_sweep, +1 for the T4 outer_loop): the orphan ``slack_answer`` is
        # never seeded (the one loop with no registry MiniLoop). The seeded set
        # equals ``iter_loops()`` — pinned by
        # tests/teatree_loops/test_seed.py::test_seeded_loop_table_matches_iter_loops.
        assert Loop.objects.count() == 22
        assert Loop.objects.filter(name="dispatch").exists()


class TestLoopBackfillSatisfiesXor(TestCase):
    """Every seeded row satisfies the FK prompt-XOR-script after the #2513 conversion."""

    def test_every_seeded_row_has_exactly_one_of_prompt_or_script(self) -> None:
        for loop in Loop.objects.all():
            assert (loop.prompt_id is not None) != bool(loop.script), loop.name

    def test_arch_review_prompt_text_migrated_to_a_prompt_row(self) -> None:
        loop = Loop.objects.get(name="arch_review")
        assert loop.prompt_id is not None
        assert loop.prompt.body != ""
        assert loop.script == ""

    def test_other_loops_run_their_own_per_loop_module(self) -> None:
        # #2513: each script loop's ``script`` is its OWN module, never the
        # retired shared ``run.py``. The DB ``script`` column is per-loop and
        # load-bearing — the seed points every default script row at its own module.
        loop = Loop.objects.get(name="dispatch")
        assert loop.script == "src/teatree/loops/dispatch/loop.py"
        assert loop.prompt_id is None
        assert not Loop.objects.filter(script="src/teatree/loops/run.py").exists()
