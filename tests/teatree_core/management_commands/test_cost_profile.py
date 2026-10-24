"""``t3 cost-profile`` — read share, resume-context cost and addressable upper bounds over real dispatches."""

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Task, TaskAttempt
from teatree.core.models.task_attempt import TaskAttemptQuerySet
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from tests.factories import TaskAttemptFactory, TaskFactory

_LONG_TOOL_CALL_MS = 300_000
_OPUS_CACHE_PREMIUM_PER_MTOK = 6.25 - 0.5


def _run(*args: str) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("cost_profile", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def _real_dispatch(task: Task, **fields: object) -> TaskAttempt:
    defaults: dict[str, object] = {
        "model": "claude-opus-4-8",
        "input_tokens": 100,
        "output_tokens": 1_000,
        "cache_read_tokens": 700,
        "cache_write_tokens": 200,
        "cost_usd": 1.0,
        "num_turns": 4,
        "context_tokens": 100_000,
        "context_window_tokens": 1_000_000,
    }
    return TaskAttemptFactory.create(task=task, **{**defaults, **fields})


class CostProfileCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.parent = TaskFactory(phase="coding")
        cls.parent_attempt = _real_dispatch(cls.parent, context_tokens=250_000, cost_usd=3.0)
        cls.child = TaskFactory(
            ticket=cls.parent.ticket,
            session=cls.parent.session,
            phase="coding",
            parent_task=cls.parent,
            session_continuation=Task.SessionContinuation.PARENT,
        )
        cls.child_attempt = _real_dispatch(
            cls.child,
            context_tokens=180_000,
            cost_usd=4.0,
            result={
                "tool_calls": [{"tool": "Bash", "duration_ms": _LONG_TOOL_CALL_MS}, {"tool": "Read", "duration_ms": 90}]
            },
        )
        cls.billed_park = TaskAttemptFactory(
            task=TaskFactory(),
            error=f"{LIMIT_PARKED_PREFIX}weekly window",
            output_tokens=500,
            cost_usd=50.0,
            exit_code=None,
        )
        cls.unknown_usage = TaskAttemptFactory(task=TaskFactory(), usage_unknown=True, output_tokens=0, cost_usd=60.0)
        cls.fresh = _real_dispatch(TaskFactory(), cost_usd=2.0)

    def _payload(self, *args: str) -> dict:
        out, _ = _run("--json", *args)
        return json.loads(out)

    def test_park_and_unknown_usage_rows_are_not_real_dispatches(self) -> None:
        payload = self._payload()

        assert payload["attempts"] == 3
        assert payload["total_cost_usd"] == pytest.approx(9.0)

    def test_read_share_is_cache_read_over_input_plus_read_plus_write(self) -> None:
        payload = self._payload()

        assert payload["read_share"]["overall"] == pytest.approx(700 / (100 + 700 + 200))

    def test_a_parent_child_carries_the_parents_last_context(self) -> None:
        item = next(entry for entry in self._payload()["addressable"] if entry["item"] == "item2_resume_context")

        assert item["flagged_attempts"] == 1
        assert item["usd_upper_bound"] == pytest.approx(4.0)

    def test_a_reattempt_carries_its_previous_attempts_context(self) -> None:
        _real_dispatch(self.parent, context_tokens=50_000, cost_usd=0.5)
        item = next(entry for entry in self._payload()["addressable"] if entry["item"] == "item2_resume_context")

        assert item["flagged_attempts"] == 2
        assert item["usd_upper_bound"] == pytest.approx(4.5)

    def test_a_long_tool_call_is_priced_and_attempts_without_trajectory_stay_unmeasured(self) -> None:
        item = next(entry for entry in self._payload()["addressable"] if entry["item"] == "item4_long_waits")

        assert item["usd_upper_bound"] == pytest.approx(180_000 * _OPUS_CACHE_PREMIUM_PER_MTOK / 1_000_000)
        assert item["measured_attempts"] == 1
        assert item["unmeasured_attempts"] == 2

    def test_since_excludes_older_attempts(self) -> None:
        TaskAttempt.objects.filter(pk=self.parent_attempt.pk).update(started_at=timezone.now() - timedelta(days=30))
        cutoff = (timezone.now() - timedelta(days=1)).isoformat()

        payload = self._payload("--since", cutoff)

        assert payload["attempts"] == 2

    def test_human_view_goes_to_stderr_and_names_the_upper_bound_label(self) -> None:
        out, err = _run()

        assert out == ""
        assert "upper bound" in err
        assert "read share" in err

    def test_a_database_read_error_exits_non_zero_naming_the_cause(self) -> None:
        err = StringIO()

        with (
            patch.object(TaskAttemptQuerySet, "real_dispatches", side_effect=OperationalError("disk I/O error")),
            pytest.raises(SystemExit) as raised,
        ):
            call_command("cost_profile", "--json", stdout=StringIO(), stderr=err)

        assert raised.value.code == 1
        assert "control DB read failed" in err.getvalue()

    def test_a_malformed_since_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit) as raised:
            _run("--since", "last tuesday")

        assert raised.value.code == 1


class CostProfileFailLoudTests(TestCase):
    def test_zero_real_dispatches_exits_non_zero_naming_the_cause(self) -> None:
        err = StringIO()

        with pytest.raises(SystemExit) as raised:
            call_command("cost_profile", "--json", stdout=StringIO(), stderr=err)

        assert raised.value.code == 1
        assert "no real dispatches" in err.getvalue()

    def test_every_context_null_exits_non_zero_naming_the_cause(self) -> None:
        _real_dispatch(TaskFactory(), context_tokens=None)
        err = StringIO()

        with pytest.raises(SystemExit) as raised:
            call_command("cost_profile", "--json", stdout=StringIO(), stderr=err)

        assert raised.value.code == 1
        assert "context_tokens" in err.getvalue()
