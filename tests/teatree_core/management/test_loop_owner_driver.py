"""Wire-level driver observability on ``t3 loop claim`` / ``owner`` (PR-26 / M9).

A pid-anchored claim that resolves no driver still SUCCEEDS but warns loud (stderr)
naming the remedies, and its JSON carries ``driverless: true``. A claim with a
detected loop runner registers the driver and stays quiet. ``--driver external`` is
the explicit override for a foreign scheduler.
"""

import io
import json
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import LoopLease
from tests._loop_principal_env import pinned_loop_principal


def _claim(*args: str, detected: str = "", **kwargs) -> tuple[str, str]:
    """Run ``loop_owner claim`` with detection stubbed to *detected*; return (stdout, stderr).

    Command flags (e.g. the ``--driver`` override) are passed through **kwargs* as
    ``driver=...`` — distinct from *detected*, which stubs ``detect_driver``.
    """
    out, err = io.StringIO(), io.StringIO()
    with (
        pinned_loop_principal("sess-x"),
        mock.patch("teatree.loop.driver_detection.detect_driver", return_value=detected),
    ):
        call_command("loop_owner", "claim", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


class TestClaimWithALoopRunnerRegisters(TestCase):
    def test_detected_loop_runner_is_written_and_no_warning(self) -> None:
        _out, err = _claim(slot="loop:dispatch", detected="loop_runner")
        assert LoopLease.objects.get(name="loop:dispatch").driver == "loop_runner"
        assert "DRIVERLESS" not in err

    def test_json_reports_the_detected_driver(self) -> None:
        out, _err = _claim(slot="loop:dispatch", detected="loop_runner", json_output=True)
        payload = json.loads(out)
        assert payload["driver"] == "loop_runner"
        assert payload["driverless"] is False


class TestClaimWithNothingWarnsLoud(TestCase):
    def test_claim_succeeds_but_warns_with_remediation_verbatim(self) -> None:
        _out, err = _claim(slot="loop:dispatch", detected="")
        # The claim itself SUCCEEDS (exit 0, OK on the human/stderr channel) — driverless is a warning, not a refusal.
        assert "OK    claimed" in err
        assert LoopLease.objects.get(name="loop:dispatch").driver == ""
        # The remedies are named verbatim; an attended session is not one of them.
        assert "t3 worker" in err
        assert "--driver external" in err
        assert "self-pump" not in err

    def test_json_marks_driverless(self) -> None:
        out, _err = _claim(slot="loop:dispatch", detected="", json_output=True)
        payload = json.loads(out)
        assert payload["driver"] == ""
        assert payload["driverless"] is True


class TestExplicitExternalOverride(TestCase):
    def test_explicit_external_driver_overrides_detection(self) -> None:
        # Detection would return loop_runner, but --driver external wins (foreign scheduler).
        _out, err = _claim(slot="loop:dispatch", detected="loop_runner", driver="external")
        assert LoopLease.objects.get(name="loop:dispatch").driver == "external"
        assert "DRIVERLESS" not in err


class TestOwnerSurfacesDriver(TestCase):
    def test_owner_json_carries_the_driver(self) -> None:
        _claim(slot="loop:dispatch", detected="loop_runner")
        out = io.StringIO()
        with pinned_loop_principal("sess-x"):
            call_command("loop_owner", "owner", slot="loop:dispatch", json_output=True, stdout=out)
        assert json.loads(out.getvalue())["driver"] == "loop_runner"

    def test_owner_text_reports_driverless(self) -> None:
        _claim(slot="loop:dispatch", detected="")
        out = io.StringIO()
        err = io.StringIO()
        with pinned_loop_principal("sess-x"):
            call_command("loop_owner", "owner", slot="loop:dispatch", stdout=out, stderr=err)
        assert "driver: DRIVERLESS" in err.getvalue()


class TestTheRetiredDriverIsRefused(TestCase):
    def test_an_explicit_self_pump_driver_is_invalid(self) -> None:
        out = io.StringIO()
        with pinned_loop_principal("sess-x"), pytest.raises(SystemExit) as exit_info:
            call_command("loop_owner", "claim", slot="loop:dispatch", driver="self_pump", stdout=out, stderr=out)
        assert exit_info.value.code == 2
        assert "invalid --driver" in out.getvalue()
