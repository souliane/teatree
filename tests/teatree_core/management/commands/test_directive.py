"""``manage.py directive`` (north-star PR-6): the deterministic intake + inspection surface.

``capture`` records a directive verbatim as a CAPTURED row — always available (the
explicit operator path is not gated by the dark loop flag). ``list`` and ``status``
are read-only inspection.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Directive


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
