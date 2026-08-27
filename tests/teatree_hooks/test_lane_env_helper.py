"""Tests for ``tests/_lane_env.py`` — the lane pin that states rather than inherits (#3973).

:class:`TestThePinOverridesTheAmbientLane` is the regression. A factory / Agent-SDK
runner exports the markers ``session_lane`` reads, so a fixture that merely MERGES its
own keys resolves to the runner's lane; under the headless authoring gate — fail-open
for every lane but a positively identified interactive CLI — ten refuse-cases then
passed by ALLOWING, and no allow-case could notice. Only CI, which exports no marker,
held the gate honest.

The ambient lane is re-created here rather than inherited, so each case reproduces a
factory runner's env on a box that has none.
"""

import os

import pytest

from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI, LANE_SDK, LANE_UNKNOWN, session_lane
from tests import _lane_env
from tests._lane_env import (
    ENTRYPOINT_ENV,
    INTERACTIVE_ENV,
    LANE_KEYS,
    SDK_VERSION_ENV,
    ambient_lane_env_stripped,
    pin_lane,
    pinned_lane,
)

_SDK_ENTRYPOINT = "sdk-py"
_SDK_VERSION = "0.2.95"


def _ambient_sdk_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env a headless factory worker leaves in place before a test states anything."""
    monkeypatch.setenv(ENTRYPOINT_ENV, _SDK_ENTRYPOINT)
    monkeypatch.setenv(SDK_VERSION_ENV, _SDK_VERSION)


class TestThePinOverridesTheAmbientLane:
    @pytest.mark.parametrize("lane", [LANE_INTERACTIVE_CLI, LANE_SDK, LANE_UNKNOWN])
    def test_every_lane_resolves_through_an_ambient_sdk_marker(
        self, monkeypatch: pytest.MonkeyPatch, lane: str
    ) -> None:
        _ambient_sdk_runner(monkeypatch)
        with pinned_lane(lane):
            assert session_lane() == lane

    def test_the_monkeypatch_form_agrees(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ambient_sdk_runner(monkeypatch)
        pin_lane(monkeypatch, LANE_INTERACTIVE_CLI)

        assert session_lane() == LANE_INTERACTIVE_CLI

    def test_extra_env_rides_alongside_the_lane(self) -> None:
        with pinned_lane(LANE_INTERACTIVE_CLI, T3_OVERLAY_NAME="probe"):
            assert os.environ["T3_OVERLAY_NAME"] == "probe"
            assert session_lane() == LANE_INTERACTIVE_CLI


class TestTheAssertionIsTheControl:
    """Without it a mis-stated env is indistinguishable from a correct one."""

    def test_a_lane_table_that_states_the_wrong_env_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrong = {LANE_INTERACTIVE_CLI: {ENTRYPOINT_ENV: _SDK_ENTRYPOINT, SDK_VERSION_ENV: _SDK_VERSION}}
        monkeypatch.setattr(_lane_env, "_LANE_ENV", wrong)

        with pytest.raises(AssertionError, match=LANE_SDK), pinned_lane(LANE_INTERACTIVE_CLI):
            pytest.fail("the pin yielded on an env stating the wrong lane")


class TestTheEnvironmentIsRestored:
    def test_an_ambient_marker_survives_the_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ambient_sdk_runner(monkeypatch)
        with pinned_lane(LANE_INTERACTIVE_CLI):
            assert INTERACTIVE_ENV in os.environ

        assert os.environ[SDK_VERSION_ENV] == _SDK_VERSION
        assert INTERACTIVE_ENV not in os.environ

    def test_a_raising_block_still_restores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ambient_sdk_runner(monkeypatch)
        failure = RuntimeError("the pinned block raised")
        with pytest.raises(RuntimeError), pinned_lane(LANE_INTERACTIVE_CLI):
            raise failure

        assert os.environ[ENTRYPOINT_ENV] == _SDK_ENTRYPOINT


class TestTheAmbientScrubIsWhatConftestApplies:
    """The autouse fixture's whole body, so this holds in CI as well as on a dev box."""

    def test_every_marker_is_removed_and_the_lane_reads_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ambient_sdk_runner(monkeypatch)
        with ambient_lane_env_stripped():
            assert [key for key in LANE_KEYS if key in os.environ] == []
            assert session_lane() == LANE_UNKNOWN

    def test_the_markers_come_back_afterwards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ambient_sdk_runner(monkeypatch)
        with ambient_lane_env_stripped():
            pass

        assert os.environ[SDK_VERSION_ENV] == _SDK_VERSION


class TestTheSuiteRunsLaneHermetic:
    def test_a_test_that_states_no_lane_sees_unknown(self) -> None:
        assert session_lane() == LANE_UNKNOWN
