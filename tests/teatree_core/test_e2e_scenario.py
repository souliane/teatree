"""The frozen authoring ``Scenario`` / ``Capture`` contract (#3329)."""

import dataclasses

import pytest

from teatree.core.e2e_scenario import Capture, Scenario


class TestCapture:
    def test_slot_only_defaults_empty_caption(self) -> None:
        capture = Capture(slot="step1")
        assert capture.slot == "step1"
        assert capture.caption == ""

    def test_is_frozen(self) -> None:
        capture = Capture(slot="step1", caption="the field")
        with pytest.raises(dataclasses.FrozenInstanceError):
            capture.slot = "other"  # type: ignore[misc]


class TestScenario:
    def test_surface_only_carries_sensible_defaults(self) -> None:
        scenario = Scenario(surface="Login")
        assert scenario.surface == "Login"
        assert scenario.title == ""
        assert scenario.steps == ()
        assert scenario.captures == ()
        assert scenario.modality == "ui"
        assert scenario.is_api is False

    def test_api_modality_flags_is_api(self) -> None:
        scenario = Scenario(surface="Contract", modality="api")
        assert scenario.is_api is True

    def test_carries_captures_and_steps(self) -> None:
        scenario = Scenario(
            surface="Login",
            steps=("open", "submit"),
            captures=(Capture(slot="step1", caption="form"),),
        )
        assert scenario.steps == ("open", "submit")
        assert scenario.captures[0].slot == "step1"

    def test_is_frozen(self) -> None:
        scenario = Scenario(surface="Login")
        with pytest.raises(dataclasses.FrozenInstanceError):
            scenario.surface = "other"  # type: ignore[misc]
