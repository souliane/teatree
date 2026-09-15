"""``t3 <overlay> questions reopen`` — the operator's recovery surface (#4748).

The drain resolvers are single-use CAS writes, so a wrongly-dropped question had no way
back: no code path cleared ``dismissed_at``, ``answer`` and ``dismiss`` both guard on it
being null, and the model is not registered in the admin. This is the one surface that
reverses an automated dismissal, and it reports which ids it could not.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.models import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str) -> tuple[str, str, int]:
    out, err, code = StringIO(), StringIO(), 0
    try:
        call_command(*args, stdout=out, stderr=err)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return out.getvalue(), err.getvalue(), code


def _drained(text: str) -> DeferredQuestion:
    row = DeferredQuestion.objects.create(question=text)
    row.mark_stale("the halted lane has since run to success", resolver_id="halt_trigger_cleared")
    return row


class TestQuestionsReopen:
    def test_a_drained_row_is_pending_again(self) -> None:
        row = _drained("How should this halt proceed?")

        out, _err, code = _call("questions", "reopen", str(row.pk), "--note", "the coding lane is still halted")

        assert code == 0
        assert f"#{row.pk}" in out
        row.refresh_from_db()
        assert row.status == "pending"

    def test_one_command_reopens_the_whole_batch(self) -> None:
        rows = [_drained(f"halt {i}?") for i in range(3)]

        _out, _err, code = _call("questions", "reopen", *[str(r.pk) for r in rows])

        assert code == 0
        for row in rows:
            row.refresh_from_db()
            assert row.status == "pending"

    def test_an_unreopenable_id_does_not_roll_back_the_others(self) -> None:
        good_a, good_b = _drained("halt a?"), _drained("halt b?")
        still_pending = DeferredQuestion.objects.create(question="halt c?")

        _out, err, code = _call("questions", "reopen", str(good_a.pk), str(still_pending.pk), str(good_b.pk))

        assert code == 0
        assert str(still_pending.pk) in err
        for row in (good_a, good_b):
            row.refresh_from_db()
            assert row.status == "pending"

    def test_a_batch_that_reopens_nothing_is_a_failure(self) -> None:
        row = DeferredQuestion.objects.create(question="already pending?")

        _out, _err, code = _call("questions", "reopen", str(row.pk))

        assert code == 1

    def test_an_unknown_id_is_reported_not_crashed(self) -> None:
        _out, err, code = _call("questions", "reopen", "999999")

        assert code == 1
        assert "999999" in err

    def test_the_reopened_row_is_listed_as_pending_again(self) -> None:
        row = _drained("How should this halt proceed?")
        _call("questions", "reopen", str(row.pk))

        out, _err, _code = _call("questions", "list", "--json")

        assert str(row.pk) in out

    def test_list_all_carries_the_dismissal_reason_a_reopen_id_is_picked_from(self) -> None:
        row = _drained("How should this halt proceed?")

        out, _err, _code = _call("questions", "list", "--all", "--json")

        assert "the halted lane has since run to success" in out
        assert str(row.pk) in out
