"""Whether a LOOP runs is the preset's opinion, never a second scalar (B1 + D6).

A preset now holds a true/false opinion on every live loop, so a per-loop `<name>_enabled`
setting is a second answer to a question already answered — and the two could disagree, which
is what "the loop is on but nothing happens" looked like from the outside.

The distinction this file pins is the one the cut turns on: a scalar that decides whether a
LOOP EXISTS duplicates the preset and goes. A probe that could not see a refusal at all
would pass the two assertions below for free, so the control is a refusal the SAME probe
does observe — a backend with no code host has no merge requests to survey.
"""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.setting_taxonomy import SettingClass, classify
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConfigSetting
from teatree.loop.scanner_factories import (
    _issue_intake_scanner_for,
    _mr_triage_scanner_for,
    _triage_assessor_scanner_for,
)

RETIRED_EXISTENCE_SCALARS = ("issue_implementer_enabled", "triage_assessor_enabled")


def _backend(name: str = "acme") -> OverlayBackends:
    host = MagicMock(spec=CodeHostBackend)
    host.current_user.return_value = "alice"
    return OverlayBackends(name=name, hosts=(host,), messaging=None, ready_labels=(), identities=("alice",))


class TestAStoredExistenceScalarNoLongerDecides(TestCase):
    """A row an operator left behind must not out-vote the preset that replaced it."""

    def test_a_stored_off_row_does_not_suppress_the_triage_assessor(self) -> None:
        ConfigSetting.objects.create(key="triage_assessor_enabled", value=False)
        assert _triage_assessor_scanner_for(_backend()) is not None

    def test_a_stored_off_row_does_not_suppress_the_issue_intake(self) -> None:
        ConfigSetting.objects.create(key="issue_implementer_enabled", value=False)
        assert _issue_intake_scanner_for(_backend()) is not None

    def test_the_probe_can_see_a_refusal_so_the_assertions_above_are_not_free(self) -> None:
        hostless = OverlayBackends(name="acme", hosts=(), messaging=None, ready_labels=())
        assert _mr_triage_scanner_for(hostless, ci_enricher=MagicMock()) is None


class TestTheScalarsAreRetiredNotMerelyUnread(TestCase):
    """Deleting the field without recording the retirement reverts the operator's value in silence."""

    def test_neither_scalar_is_a_live_key(self) -> None:
        for key in RETIRED_EXISTENCE_SCALARS:
            assert key not in ALL_KNOWN_CONFIG_SETTINGS, key

    def test_each_scalar_answers_as_retired(self) -> None:
        for key in RETIRED_EXISTENCE_SCALARS:
            assert classify(key).classes == {SettingClass.RETIRED}, key
