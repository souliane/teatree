"""``manage.py directive`` (north-star PR-6 + PR-7): intake, drive, and inspection.

``capture`` records a directive verbatim as a CAPTURED row — always available (the
explicit operator path is not gated by the dark loop flag). ``list`` / ``status`` /
``history`` are read-only inspection. ``tick`` is the off-live-tick cron (SKIPs while
the ``directive_loop`` Loop row is disabled — the shipped state). ``resolve-revert``
closes a REVERT_PENDING directive to terminal REVERTED with the config rolled back.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting, DeferredQuestion, Directive, FactoryScoreSnapshot
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope

_SCOPE = "t3-teatree"
_KEY = "max_open_prs_per_repo_per_ticket"


def _revert_pending() -> Directive:
    directive = Directive.objects.capture("max 1 MR", source=Directive.Source.CLI, scope_overlay=_SCOPE)
    directive.record_interpretation(
        sketch_from_envelope(valid_envelope(kind="activation_only", acceptance_tests=[])), constraint_statement="c"
    )
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    directive.skip_to_configuring(
        baseline_snapshot=FactoryScoreSnapshot.objects.create(
            overlay="", window_days=7, recipe_sha="s", aggregate=0.7, verdict="ok", coverage=1.0, coverage_floor=0.6
        )
    )
    directive.begin_verifying()
    directive.request_revert(reason="regression")
    return directive


class TestCaptureCommand(TestCase):
    def test_capture_records_a_captured_directive(self) -> None:
        out = StringIO()
        call_command("directive", "capture", "always open MRs as drafts for overlay X", stdout=out)
        directive = Directive.objects.get()
        assert directive.state == Directive.State.CAPTURED
        assert directive.raw_text == "always open MRs as drafts for overlay X"
        assert directive.source == Directive.Source.CLI
        assert f"#{directive.pk}" in out.getvalue()

    def test_capture_stores_the_scope_overlay(self) -> None:
        call_command("directive", "capture", "cap PRs", scope="t3-teatree")
        assert Directive.objects.get().scope_overlay == "t3-teatree"

    def test_capture_refuses_blank_text(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("directive", "capture", "   ")
        assert exc.value.code == 1
        assert not Directive.objects.exists()


class TestListAndStatus(TestCase):
    def test_list_shows_recorded_directives(self) -> None:
        Directive.objects.capture("draft MRs for X", source=Directive.Source.CLI)
        out = StringIO()
        call_command("directive", "list", stdout=out)
        assert "draft MRs for X" in out.getvalue()

    def test_status_prints_state_and_text(self) -> None:
        directive = Directive.objects.capture("cap PRs at 1", source=Directive.Source.CLI)
        out = StringIO()
        call_command("directive", "status", str(directive.pk), stdout=out)
        rendered = out.getvalue()
        assert "captured" in rendered
        assert "cap PRs at 1" in rendered

    def test_status_refuses_an_unknown_directive(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("directive", "status", "999999")
        assert exc.value.code == 1


class TestTickCommand(TestCase):
    def test_tick_skips_while_the_loop_row_is_disabled(self) -> None:
        # The shipped state — no enabled directive_loop Loop row (QUADRUPLE-OFF layer 2).
        out = StringIO()
        call_command("directive", "tick", stdout=out)
        assert "SKIP" in out.getvalue()
        assert "disabled" in out.getvalue()


class TestResolveRevertCommand(TestCase):
    def test_resolve_revert_reaches_reverted_and_clears_config(self) -> None:
        directive = _revert_pending()
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)
        out = StringIO()
        call_command("directive", "resolve-revert", str(directive.pk), revert_sha="beef", stdout=out)
        directive.refresh_from_db()
        assert directive.state == Directive.State.REVERTED
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) is None
        assert "reverted" in out.getvalue()

    def test_resolve_revert_refuses_a_non_revert_pending_directive(self) -> None:
        directive = Directive.objects.capture("x", source=Directive.Source.CLI)
        with pytest.raises(SystemExit) as exc:
            call_command("directive", "resolve-revert", str(directive.pk))
        assert exc.value.code == 1

    def test_resolve_revert_refuses_an_unknown_directive(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("directive", "resolve-revert", "999999")
        assert exc.value.code == 1


class TestHistoryCommand(TestCase):
    def test_history_shows_directives_with_decisions(self) -> None:
        directive = Directive.objects.capture("draft MRs for X", source=Directive.Source.CLI)
        directive.reject("uninterpretable")
        out = StringIO()
        call_command("directive", "history", stdout=out)
        rendered = out.getvalue()
        assert f"#{directive.pk}" in rendered
        assert "uninterpretable" in rendered
