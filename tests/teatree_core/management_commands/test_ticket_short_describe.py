"""Tests for ``manage.py ticket_short_describe`` (#1156).

The command drives one clean-room, cheap-tier turn through the shared
one-shot seam (:func:`teatree.agents.one_shot.run_one_shot`) — so the summary
follows a swapped tier-model DB row and works off-Claude, with no
``teatree.eval`` import on the production path. These tests inject a fake
summary by patching ``run_one_shot`` so the suite never invokes a real LLM,
and pin that NO subprocess is ever spawned on the describe path. The seam's
own clean-room + failure contract is proved in
``tests/teatree_agents/test_one_shot.py``.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.management.commands.ticket_short_describe import (
    _FALLBACK_LEN,
    _describe_all_missing,
    _generate_short_description,
    _summarize,
    _truncation_fallback,
    describe_ticket,
)
from teatree.core.models import Ticket

_SUMMARIZE = "teatree.core.management.commands.ticket_short_describe._summarize"
_RUN_ONE_SHOT = "teatree.core.management.commands.ticket_short_describe.run_one_shot"


class TestTruncationFallback:
    def test_short_title_passes_through_untouched(self) -> None:
        assert _truncation_fallback("short") == "short"

    def test_exactly_at_limit_passes_through(self) -> None:
        title = "x" * _FALLBACK_LEN
        assert _truncation_fallback(title) == title

    def test_over_limit_gets_ellipsis_suffix(self) -> None:
        title = "y" * (_FALLBACK_LEN + 20)
        result = _truncation_fallback(title)
        assert len(result) == _FALLBACK_LEN
        assert result.endswith("…")

    def test_unicode_title_truncates_safely(self) -> None:
        title = "naïve " * 20  # plenty long
        result = _truncation_fallback(title)
        assert len(result) <= _FALLBACK_LEN
        assert result.endswith("…")


class TestGenerateShortDescription:
    def test_blank_title_returns_empty(self) -> None:
        assert _generate_short_description("") == ""
        assert _generate_short_description("   ") == ""

    def test_summary_from_model_is_truncated_to_eighty(self) -> None:
        long_summary = "x" * 200
        with patch(_SUMMARIZE, return_value=long_summary):
            result = _generate_short_description("Some ticket title")
        assert len(result) == 80
        assert result == "x" * 80

    def test_falls_back_to_truncation_when_model_returns_empty(self) -> None:
        title = "z" * 100
        with patch(_SUMMARIZE, return_value=""):
            result = _generate_short_description(title)
        assert result.endswith("…")
        assert len(result) == _FALLBACK_LEN

    def test_short_title_with_failed_model_returns_title_as_is(self) -> None:
        with patch(_SUMMARIZE, return_value=""):
            assert _generate_short_description("hi") == "hi"


class TestSummarize:
    """The summary path is the shared one-shot seam — cheap tier, no hardcoded model id."""

    def test_seam_failure_returns_empty(self) -> None:
        # The seam returns None on ANY failure (missing binary, credential
        # problem, timeout, backend error) → the caller degrades to truncation.
        with patch(_RUN_ONE_SHOT, return_value=None):
            assert _summarize("anything") == ""

    def test_turn_rides_the_cheap_tier(self) -> None:
        # Anti-hardcode pin: the summary resolves the CHEAP tier through the
        # seam rather than naming a concrete model id.
        with patch(_RUN_ONE_SHOT, return_value="ok") as one_shot:
            assert _summarize("title") == "ok"
        (_prompt, spec), _kwargs = one_shot.call_args
        assert spec.tier == "cheap"
        assert spec.max_turns == 1

    def test_returns_last_non_blank_line_stripped(self) -> None:
        with patch(_RUN_ONE_SHOT, return_value='preamble\n"Final summary"'):
            assert _summarize("title") == "Final summary"

    def test_summary_path_never_spawns_a_subprocess(self) -> None:
        """The describe path never shells a subprocess — the seam is the one runner.

        With ``run_one_shot`` stubbed to a canned summary, a healthy run touches
        neither ``subprocess`` egress primitive (``Popen`` / ``run``), so a
        reintroduced ``spawn(["claude", "-p", …])`` on this path would be caught.
        """
        spawn_calls: list[object] = []

        def _record(*args: object, **_kwargs: object) -> object:
            spawn_calls.append(args)
            return None

        with (
            patch(_RUN_ONE_SHOT, return_value="dogfood smoke scanner"),
            patch("subprocess.Popen", side_effect=_record),
            patch("subprocess.run", side_effect=_record),
        ):
            result = _summarize("implement the dogfood smoke scanner")

        assert result == "dogfood smoke scanner"
        assert spawn_calls == []


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDescribeOne(TestCase):
    def test_missing_ticket_emits_noop_and_exits_one(self) -> None:
        captured: list[str] = []
        with pytest.raises(SystemExit) as excinfo:
            describe_ticket(99999, stdout_write=captured.append)
        assert excinfo.value.code == 1
        assert any("no ticket with id=99999" in line for line in captured)

    def test_ticket_without_title_is_noop(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={})
        captured: list[str] = []
        describe_ticket(ticket.pk, stdout_write=captured.append)
        assert any("has no extra['issue_title']" in line for line in captured)
        ticket.refresh_from_db()
        assert ticket.short_description == ""

    def test_ticket_with_title_gets_described(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "implement the dogfood smoke scanner"},
        )
        captured: list[str] = []
        with patch(_SUMMARIZE, return_value="dogfood smoke scanner"):
            describe_ticket(ticket.pk, stdout_write=captured.append)
        ticket.refresh_from_db()
        assert ticket.short_description == "dogfood smoke scanner"


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDescribeAllMissing(TestCase):
    def test_skips_tickets_without_title(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", extra={})
        captured: list[str] = []
        _describe_all_missing(stdout_write=captured.append)
        # Only the DONE summary is captured — nothing else.
        assert any(line.startswith("DONE") for line in captured)

    def test_describes_each_ticket_with_title(self) -> None:
        ticket_a = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "implement provision smoke (#1308)"},
        )
        ticket_b = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "merge clear keystone"},
        )
        with patch(_SUMMARIZE, side_effect=["provision smoke", "merge clear"]):
            captured: list[str] = []
            _describe_all_missing(stdout_write=captured.append)
        ticket_a.refresh_from_db()
        ticket_b.refresh_from_db()
        assert ticket_a.short_description == "provision smoke"
        assert ticket_b.short_description == "merge clear"
        assert any("DONE  described 2 ticket(s)" in line for line in captured)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestCommandDescribeMethod(TestCase):
    """Cover the ``Command.describe`` argument-validation branches directly."""

    def _command(self):
        from teatree.core.management.commands.ticket_short_describe import Command  # noqa: PLC0415

        return Command()

    def test_describe_rejects_both_flags(self) -> None:
        cmd = self._command()
        with pytest.raises(SystemExit) as excinfo:
            cmd.describe(ticket_id=1, all_missing=True)
        assert excinfo.value.code == 2

    def test_describe_rejects_no_flags(self) -> None:
        cmd = self._command()
        with pytest.raises(SystemExit) as excinfo:
            cmd.describe(ticket_id=0, all_missing=False)
        assert excinfo.value.code == 2

    def test_describe_with_ticket_id_calls_describe_ticket(self) -> None:
        cmd = self._command()
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "test ticket"},
        )
        with patch(_SUMMARIZE, return_value="test ticket"):
            cmd.describe(ticket_id=ticket.pk, all_missing=False)
        ticket.refresh_from_db()
        assert ticket.short_description == "test ticket"

    def test_describe_with_all_missing_calls_backfill(self) -> None:
        cmd = self._command()
        Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "backfill candidate"},
        )
        with patch(_SUMMARIZE, return_value="backfilled"):
            cmd.describe(ticket_id=0, all_missing=True)
