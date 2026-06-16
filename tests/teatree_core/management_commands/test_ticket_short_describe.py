"""Tests for ``manage.py ticket_short_describe`` (#1156).

The command drives one in-process ``claude_agent_sdk.query`` turn in
production (#2204 cutover — no ``claude -p`` subprocess). These tests
inject a fake summary (via ``shutil.which`` and ``_describe`` patches)
so the suite never invokes a real LLM, and pin that NO claude-binary
subprocess is ever spawned on the describe path.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.core.management.commands.ticket_short_describe as describe_mod
from teatree.core.management.commands.ticket_short_describe import (
    _FALLBACK_LEN,
    _claude_summarize,
    _describe_all_missing,
    _describe_one,
    _generate_short_description,
    _truncation_fallback,
)
from teatree.core.models import Ticket


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

    def test_summary_from_claude_is_truncated_to_eighty(self) -> None:
        long_summary = "x" * 200
        with patch(
            "teatree.core.management.commands.ticket_short_describe._claude_summarize",
            return_value=long_summary,
        ):
            result = _generate_short_description("Some ticket title")
        assert len(result) == 80
        assert result == "x" * 80

    def test_falls_back_to_truncation_when_claude_returns_empty(self) -> None:
        title = "z" * 100
        with patch(
            "teatree.core.management.commands.ticket_short_describe._claude_summarize",
            return_value="",
        ):
            result = _generate_short_description(title)
        assert result.endswith("…")
        assert len(result) == _FALLBACK_LEN

    def test_short_title_with_failed_claude_returns_title_as_is(self) -> None:
        with patch(
            "teatree.core.management.commands.ticket_short_describe._claude_summarize",
            return_value="",
        ):
            assert _generate_short_description("hi") == "hi"


class TestClaudeSummarizer:
    """The summary path is the in-process Agent SDK — never a ``claude -p`` subprocess (#2204)."""

    def test_missing_binary_returns_empty(self) -> None:
        with patch.object(describe_mod.shutil, "which", return_value=None):
            assert _claude_summarize("anything") == ""

    def test_sdk_failure_returns_empty(self) -> None:
        """A crash inside the SDK turn falls through to empty (not raise)."""
        with (
            patch.object(describe_mod.shutil, "which", return_value="/usr/local/bin/claude"),
            patch.object(describe_mod, "_describe", side_effect=RuntimeError("sdk boom")),
        ):
            assert _claude_summarize("anything") == ""

    def test_timeout_returns_empty(self) -> None:
        with (
            patch.object(describe_mod.shutil, "which", return_value="/usr/local/bin/claude"),
            patch.object(describe_mod, "_describe", side_effect=TimeoutError),
        ):
            assert _claude_summarize("title") == ""

    def test_returns_last_non_blank_line_stripped(self) -> None:
        with (
            patch.object(describe_mod.shutil, "which", return_value="/usr/local/bin/claude"),
            patch.object(describe_mod, "_describe", return_value='preamble\n"Final summary"\n'),
        ):
            assert _claude_summarize("title") == "Final summary"

    def test_empty_output_returns_empty(self) -> None:
        with (
            patch.object(describe_mod.shutil, "which", return_value="/usr/local/bin/claude"),
            patch.object(describe_mod, "_describe", return_value=""),
        ):
            assert _claude_summarize("title") == ""

    def test_summary_path_never_spawns_a_claude_subprocess(self) -> None:
        """RED before the #2204 cutover: assert NO ``claude -p`` subprocess is launched.

        The describe path drives the in-process SDK only. We record the two
        ``subprocess`` egress *primitives* (``subprocess.Popen`` /
        ``subprocess.run``) rather than the ``teatree.utils.run`` wrappers,
        and that placement is what makes the guard real: every teatree wrapper
        bottoms out in one of these primitives — ``spawn`` calls
        ``subprocess.Popen`` at the module level (``run.py``), so a
        reintroduced ``spawn(["claude", "-p", …])`` is recorded here.
        Recording ``teatree.utils.run.spawn`` instead would be a no-op, since
        the pre-cutover call site imported it by name
        (``from teatree.utils.run import spawn``) — a binding a wrapper-level
        patch can't reach. The SDK boundary (``_describe``) is stubbed to a
        canned summary; a healthy run returns it and touches neither primitive.
        """
        spawn_calls: list[object] = []

        def _record(*args: object, **kwargs: object) -> object:
            spawn_calls.append(args)
            return None

        with (
            patch.object(describe_mod.shutil, "which", return_value="/usr/local/bin/claude"),
            patch.object(describe_mod, "_describe", return_value="dogfood smoke scanner"),
            patch("subprocess.Popen", side_effect=_record),
            patch("subprocess.run", side_effect=_record),
        ):
            result = _claude_summarize("implement the dogfood smoke scanner")

        assert result == "dogfood smoke scanner"
        assert spawn_calls == []


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDescribeOne(TestCase):
    def test_missing_ticket_emits_noop_and_exits_one(self) -> None:
        captured: list[str] = []
        with pytest.raises(SystemExit) as excinfo:
            _describe_one(99999, stdout_write=captured.append)
        assert excinfo.value.code == 1
        assert any("no ticket with id=99999" in line for line in captured)

    def test_ticket_without_title_is_noop(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={})
        captured: list[str] = []
        _describe_one(ticket.pk, stdout_write=captured.append)
        assert any("has no extra['issue_title']" in line for line in captured)
        ticket.refresh_from_db()
        assert ticket.short_description == ""

    def test_ticket_with_title_gets_described(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "implement the dogfood smoke scanner"},
        )
        captured: list[str] = []
        with patch(
            "teatree.core.management.commands.ticket_short_describe._claude_summarize",
            return_value="dogfood smoke scanner",
        ):
            _describe_one(ticket.pk, stdout_write=captured.append)
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
        with patch(
            "teatree.core.management.commands.ticket_short_describe._claude_summarize",
            side_effect=["provision smoke", "merge clear"],
        ):
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

    def test_describe_with_ticket_id_calls_describe_one(self) -> None:
        cmd = self._command()
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "test ticket"},
        )
        with patch(
            "teatree.core.management.commands.ticket_short_describe._claude_summarize",
            return_value="test ticket",
        ):
            cmd.describe(ticket_id=ticket.pk, all_missing=False)
        ticket.refresh_from_db()
        assert ticket.short_description == "test ticket"

    def test_describe_with_all_missing_calls_backfill(self) -> None:
        cmd = self._command()
        Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "backfill candidate"},
        )
        with patch(
            "teatree.core.management.commands.ticket_short_describe._claude_summarize",
            return_value="backfilled",
        ):
            cmd.describe(ticket_id=0, all_missing=True)
