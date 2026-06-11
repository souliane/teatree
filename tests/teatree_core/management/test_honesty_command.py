"""``t3 <overlay> honesty escalate`` records a situational honesty escalation (#2263).

The agent-facing write seam for the §43 honesty rule. ``escalate`` validates the
``--reason`` against the four triggers, resolves the session id (explicit
``--session`` or the active session), and writes an idempotent
:class:`HonestyEscalation` row.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.models import HonestyEscalation

pytestmark = pytest.mark.django_db

_SESSION = "99999999-aaaa-bbbb-cccc-dddddddddddd"


def _run(*args: str) -> str:
    out = StringIO()
    call_command("honesty", "escalate", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


class TestHonestyEscalateCommand:
    def test_escalate_records_row_for_explicit_session(self) -> None:
        _run("--reason", "user_asked", "--session", _SESSION)
        row = HonestyEscalation.objects.get(session_id=_SESSION)
        assert row.reason == HonestyEscalation.Reason.USER_ASKED
        assert HonestyEscalation.is_active(_SESSION) is True

    def test_escalate_with_task_scopes_the_row(self) -> None:
        _run("--reason", "shipped_incomplete", "--session", _SESSION, "--task", "42")
        row = HonestyEscalation.objects.get(session_id=_SESSION)
        assert row.task_id == 42
        assert row.reason == HonestyEscalation.Reason.SHIPPED_INCOMPLETE

    def test_repeat_escalation_is_idempotent(self) -> None:
        _run("--reason", "user_asked", "--session", _SESSION)
        _run("--reason", "user_asked", "--session", _SESSION)
        assert HonestyEscalation.objects.filter(session_id=_SESSION).count() == 1

    def test_invalid_reason_refused(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command(
                "honesty", "escalate", "--reason", "bogus", "--session", _SESSION, stdout=StringIO(), stderr=StringIO()
            )
        assert exc.value.code == 2
        assert HonestyEscalation.objects.filter(session_id=_SESSION).count() == 0

    def test_no_session_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No --session and no resolvable active session id → refused (exit 1).
        import teatree.core.management.commands.honesty as honesty_mod  # noqa: PLC0415

        monkeypatch.setattr(honesty_mod, "current_session_id", lambda: "")
        with pytest.raises(SystemExit) as exc:
            call_command("honesty", "escalate", "--reason", "user_asked", stdout=StringIO(), stderr=StringIO())
        assert exc.value.code == 1
