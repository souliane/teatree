"""Tests for ``t3 availability`` and ``t3 questions`` management commands (#58)."""

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from typer.testing import CliRunner

from teatree.core.availability import MODE_AWAY, MODE_PRESENT, load_override
from teatree.core.management.commands.availability import Command as AvailabilityCommand
from teatree.core.models.deferred_question import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "availability_override.json"
    monkeypatch.setattr("teatree.core.availability.override_path", lambda: target)
    return target


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


class TestAvailabilityCommand:
    def test_away_writes_override(self, override_file: Path) -> None:
        out = _call("availability", "away")
        assert "mode=away" in out
        loaded = load_override()
        assert loaded is not None
        assert loaded.mode == MODE_AWAY

    def test_away_with_until_persists_expiry(self, override_file: Path) -> None:
        until = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
        _call("availability", "away", "--until", until)
        loaded = load_override()
        assert loaded is not None
        assert loaded.until is not None

    def test_present_writes_override(self, override_file: Path) -> None:
        out = _call("availability", "present")
        assert "mode=present" in out
        loaded = load_override()
        assert loaded is not None
        assert loaded.mode == MODE_PRESENT

    def test_auto_clears_override(self, override_file: Path) -> None:
        _call("availability", "away")
        assert load_override() is not None
        out = _call("availability", "auto")
        assert "cleared" in out or "mode=" in out
        assert load_override() is None

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
