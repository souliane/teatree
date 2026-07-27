"""Tests for ``t3 availability`` and ``t3 questions`` management commands (#58)."""

import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from django.core.management import call_command
from typer.testing import CliRunner

from teatree.core.management.commands.availability import AvailabilityPayload
from teatree.core.management.commands.availability import Command as AvailabilityCommand
from teatree.core.management.commands.questions import DeferredQuestionRow
from teatree.core.mode_resolution import resolve_active_mode
from teatree.core.models import Mode, ModeOverride
from teatree.core.models.deferred_question import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "availability_override.json"
    monkeypatch.setattr("teatree.core.availability.override_path", lambda: target)
    # The availability aliases now set a ModeOverride to a merged mode; seed the
    # three modes the aliases target (offline is migration-seeded, but keep it
    # explicit so the test is self-contained).
    Mode.objects.update_or_create(
        name="offline", defaults={"entries": {}, "defers_questions": True, "pauses_self_pump": True}
    )
    Mode.objects.update_or_create(name="unattended", defaults={"entries": {}, "defers_questions": True})
    Mode.objects.update_or_create(name="engaged", defaults={"entries": {}, "defers_questions": False})
    return target


def _call(*args: str) -> str:
    """Both channels merged: these are CONTENT tests, not channel tests.

    Converted verbs route their human view to stderr through the machine-output
    seam while unconverted siblings still return it for stdout, so a content
    assertion must read both. The channel split itself is asserted by the
    dedicated tests below and by ``tests/quality/test_machine_output_seam.py``.
    """
    buf = StringIO()
    call_command(*args, stdout=buf, stderr=buf)
    return buf.getvalue()


class TestAvailabilityCommand:
    def test_away_sets_the_offline_mode_override(self, override_file: Path) -> None:
        out = _call("availability", "away")
        assert "mode=offline" in out
        assert ModeOverride.objects.current().preset_name == "offline"
        assert resolve_active_mode().name == "offline"

    def test_away_with_until_persists_expiry(self, override_file: Path) -> None:
        until = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
        _call("availability", "away", "--until", until)
        assert ModeOverride.objects.current().until is not None

    def test_present_sets_the_engaged_mode_override(self, override_file: Path) -> None:
        out = _call("availability", "present")
        assert "mode=engaged" in out
        assert resolve_active_mode().name == "engaged"

    def test_auto_clears_override(self, override_file: Path) -> None:
        _call("availability", "away")
        assert ModeOverride.objects.current() is not None
        out = _call("availability", "auto")
        assert "cleared" in out or "mode=" in out
        assert ModeOverride.objects.current() is None

    def test_show_prints_current_resolution(self, override_file: Path) -> None:
        out = _call("availability", "show")
        assert "availability:" in out
        assert "source=" in out

    def test_invalid_until_is_rejected(self, override_file: Path) -> None:
        # Invalid ISO8601 string is rejected via typer.BadParameter.
        runner = CliRunner()
        result = runner.invoke(AvailabilityCommand().typer_app, ["away", "--until", "not-a-date"])
        assert result.exit_code != 0


class TestQuestionsCommand:
    def test_list_empty(self) -> None:
        out = _call("questions", "list")
        assert "no deferred" in out

    def test_record_then_list(self) -> None:
        _call("questions", "record", "Should I ship?")
        out = _call("questions", "list")
        assert "Should I ship?" in out
        assert DeferredQuestion.objects.filter(question="Should I ship?").exists()

    def test_answer_resolves_and_audits(self) -> None:
        _call("questions", "record", "Ship?")
        row = DeferredQuestion.objects.get(question="Ship?")
        out = _call("questions", "answer", str(row.pk), "yes")
        assert f"answered #{row.pk}" in out
        row.refresh_from_db()
        assert row.answered_at is not None
        assert row.audits.filter(action="answered").exists()

    def test_dismiss_resolves_and_audits(self) -> None:
        _call("questions", "record", "Drop?")
        row = DeferredQuestion.objects.get(question="Drop?")
        out = _call("questions", "dismiss", str(row.pk), "--reason", "stale")
        assert f"dismissed #{row.pk}" in out
        row.refresh_from_db()
        assert row.dismissed_at is not None
        assert row.audits.filter(action="dismissed").exists()


class TestMachineOutputChannel:
    """``availability show`` / ``questions list`` are seam verbs: stdout is JSON or nothing."""

    @staticmethod
    def _channels(*args: str) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        call_command(*args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    @pytest.mark.usefixtures("override_file")
    def test_availability_show_human_leaves_stdout_empty(self) -> None:
        out, err = self._channels("availability", "show")
        assert out == ""
        assert "availability:" in err

    @pytest.mark.usefixtures("override_file")
    def test_availability_show_json_is_stdout_only(self) -> None:
        out, err = self._channels("availability", "show", "--json")
        assert set(json.loads(out)) == {"mode", "source"}
        assert err == ""

    def test_questions_list_human_leaves_stdout_empty(self) -> None:
        out, err = self._channels("questions", "list")
        assert out == ""
        assert "no deferred questions." in err

    def test_questions_list_json_is_stdout_only(self) -> None:
        out, err = self._channels("questions", "list", "--json")
        assert json.loads(out) == []
        assert err == ""

    @pytest.mark.usefixtures("override_file")
    def test_show_returns_exactly_the_declared_payload_shape(self) -> None:
        payload = call_command("availability", "show", stdout=StringIO(), stderr=StringIO())
        assert set(payload) == set(AvailabilityPayload.__annotations__)

    def test_questions_list_rows_match_the_declared_shape(self) -> None:
        call_command("questions", "record", "Ship?", stdout=StringIO(), stderr=StringIO())
        rows = cast(
            "list[DeferredQuestionRow]", call_command("questions", "list", stdout=StringIO(), stderr=StringIO())
        )
        assert [set(row) for row in rows] == [set(DeferredQuestionRow.__annotations__)]
