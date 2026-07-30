"""PlanArtifact.record() adequacy enforcement (SELFCATCH-3, anti-vacuity proof a).

Under ``require_plan_adequacy`` a NEW row needs a 40-char base_sha AND a complete
four-section manifest — a scope+acceptance-only thin spec is REFUSED before any
row is written. Flag OFF is byte-identical to today (generic FSM green). The
audited bypass carve-out stays: an ``is_bypass`` write records an all-negatives
manifest so ``plan()`` still advances end-to-end.
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.models import Ticket
from teatree.core.models import plan_artifact as plan_artifact_module
from teatree.core.models.plan_adequacy import all_negated_adequacy
from teatree.core.models.plan_artifact import PlanArtifact

_FORTY_HEX = "a" * 40


@contextlib.contextmanager
def _flag(*, required: bool) -> Iterator[None]:
    with patch.object(
        plan_artifact_module,
        "get_effective_settings",
        return_value=UserSettings(require_plan_adequacy=required),
    ):
        yield


def _full_adequacy() -> dict:
    return {
        "design": {"content": "record base_sha + adequacy on PlanArtifact"},
        "integration_seams": {"content": ["src/teatree/core/gates/plan_currency_gate.py"]},
        "edge_cases": {"content": ["legacy blank-sha rows"]},
        "test_strategy": {"content": "red-first thin-spec refusal"},
    }


class TestFlagOff(TestCase):
    """Opt-in default: with the flag off, record() is exactly today's behaviour."""

    def test_thin_plan_records_when_flag_off(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=False):
            artifact = PlanArtifact.record(ticket=ticket, plan_text="scope: X\nacceptance: Y", recorded_by="op")
        assert artifact.pk is not None
        assert artifact.base_sha == ""
        assert artifact.adequacy == {}


class TestFlagOnEnforcement(TestCase):
    def test_thin_spec_is_refused_on_the_adequacy_dimension(self) -> None:
        # A scope+acceptance spec (a valid base but no seams/edge-cases/test-strategy).
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=True), pytest.raises(ValueError, match="four-section adequacy manifest"):
            PlanArtifact.record(
                ticket=ticket, plan_text="scope: X\nacceptance: Y", recorded_by="op", base_sha=_FORTY_HEX
            )
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 0  # refuse-before-write

    def test_fully_thin_plan_is_refused(self) -> None:
        # No base, no adequacy — a thin spec is refused outright (base_sha checked first).
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=True), pytest.raises(ValueError, match="base_sha"):
            PlanArtifact.record(ticket=ticket, plan_text="scope: X\nacceptance: Y", recorded_by="op")
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 0

    def test_missing_base_sha_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=True), pytest.raises(ValueError, match="base_sha"):
            PlanArtifact.record(ticket=ticket, plan_text="real plan", recorded_by="op", adequacy=_full_adequacy())

    def test_short_base_sha_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=True), pytest.raises(ValueError, match="base_sha"):
            PlanArtifact.record(
                ticket=ticket, plan_text="real plan", recorded_by="op", base_sha="abc123", adequacy=_full_adequacy()
            )

    def test_a_single_silent_section_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        manifest = _full_adequacy()
        manifest["test_strategy"] = {}  # silent — never passes
        with _flag(required=True), pytest.raises(ValueError, match="four-section"):
            PlanArtifact.record(
                ticket=ticket, plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=manifest
            )

    def test_adequate_bound_plan_is_recorded(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=True):
            artifact = PlanArtifact.record(
                ticket=ticket, plan_text="real plan", recorded_by="op", base_sha=_FORTY_HEX, adequacy=_full_adequacy()
            )
        assert artifact.base_sha == _FORTY_HEX
        assert artifact.adequacy["integration_seams"]["content"] == ["src/teatree/core/gates/plan_currency_gate.py"]


class TestBypassCarveOut(TestCase):
    def test_bypass_exempt_from_enforcement_records_all_negatives(self) -> None:
        """An audited bypass records an all-negatives manifest so plan() still advances."""
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=True):
            artifact = PlanArtifact.record_bypass(
                ticket=ticket, plan_text="[audited bypass by alice] urgent hotfix", recorded_by="alice"
            )
        assert artifact.pk is not None
        assert artifact.adequacy == dict(all_negated_adequacy("[audited bypass by alice] urgent hotfix"))

    def test_bypass_still_requires_non_blank_text_and_author(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with _flag(required=True), pytest.raises(ValueError, match="plan_text is required"):
            PlanArtifact.record_bypass(ticket=ticket, plan_text="  ", recorded_by="alice")


class TestBaselineValidationUnchanged(TestCase):
    """The pre-existing blank-text / blank-author refusals are preserved."""

    def test_blank_plan_text_still_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with pytest.raises(ValueError, match="plan_text is required"):
            PlanArtifact.record(ticket=ticket, plan_text="   ", recorded_by="op")

    def test_blank_author_still_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        with pytest.raises(ValueError, match="recorded_by is required"):
            PlanArtifact.record(ticket=ticket, plan_text="plan", recorded_by="  ")
