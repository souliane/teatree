"""Wire-level driver observability on ``t3 loop claim`` / ``owner`` (PR-26 / M9).

A pid-anchored claim that resolves no driver still SUCCEEDS but warns loud (stderr)
naming the three remedies, and its JSON carries ``driverless: true``. A claim with
a detected self-pump registers the driver and stays quiet. ``--driver external`` is
the explicit override for a foreign scheduler.
"""

import io
import json
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import LoopLease


def _claim(*args: str, detected: str = "", **kwargs) -> tuple[str, str]:
    """Run ``loop_owner claim`` with detection stubbed to *detected*; return (stdout, stderr).

    Command flags (e.g. the ``--driver`` override) are passed through **kwargs* as
    ``driver=...`` — distinct from *detected*, which stubs ``detect_driver``.
    """
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch("teatree.loop.session_identity.current_session_id", return_value="sess-x"),
        mock.patch("teatree.loop.driver_detection.detect_driver", return_value=detected),
    ):
        call_command("loop_owner", "claim", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


class TestClaimWithSelfPumpRegisters(TestCase):
    def test_detected_self_pump_is_written_and_no_warning(self) -> None:
        _out, err = _claim(slot="loop:dispatch", detected="self_pump")
        assert LoopLease.objects.get(name="loop:dispatch").driver == "self_pump"
        assert "DRIVERLESS" not in err

    def test_json_reports_the_detected_driver(self) -> None:
        out, _err = _claim(slot="loop:dispatch", detected="self_pump", json_output=True)
        payload = json.loads(out)
        assert payload["driver"] == "self_pump"
        assert payload["driverless"] is False


class TestClaimWithNothingWarnsLoud(TestCase):
    def test_claim_succeeds_but_warns_with_remediation_verbatim(self) -> None:
        out, err = _claim(slot="loop:dispatch", detected="")
        # The claim itself SUCCEEDS (exit 0, OK on stdout) — driverless is a warning, not a refusal.
        assert "OK    claimed" in out
        assert LoopLease.objects.get(name="loop:dispatch").driver == ""
        # The three remedies are named verbatim.
        assert "t3 worker" in err
        assert "--driver external" in err
        assert "self-pump" in err

    def test_json_marks_driverless(self) -> None:
        out, _err = _claim(slot="loop:dispatch", detected="", json_output=True)
        payload = json.loads(out)
        assert payload["driver"] == ""
        assert payload["driverless"] is True


class TestExplicitExternalOverride(TestCase):
    def test_explicit_external_driver_overrides_detection(self) -> None:
        # Detection would return self_pump, but --driver external wins (foreign scheduler).
        _out, err = _claim(slot="loop:dispatch", detected="self_pump", driver="external")
        assert LoopLease.objects.get(name="loop:dispatch").driver == "external"
        assert "DRIVERLESS" not in err


class TestOwnerSurfacesDriver(TestCase):
    def test_owner_json_carries_the_driver(self) -> None:
        _claim(slot="loop:dispatch", detected="loop_runner")
        out = io.StringIO()
        with mock.patch("teatree.loop.session_identity.current_session_id", return_value="sess-x"):
            call_command("loop_owner", "owner", slot="loop:dispatch", json_output=True, stdout=out)
        assert json.loads(out.getvalue())["driver"] == "loop_runner"

    def test_owner_text_reports_driverless(self) -> None:
        _claim(slot="loop:dispatch", detected="")
        out = io.StringIO()
        with mock.patch("teatree.loop.session_identity.current_session_id", return_value="sess-x"):
            call_command("loop_owner", "owner", slot="loop:dispatch", stdout=out)
        assert "driver: DRIVERLESS" in out.getvalue()
