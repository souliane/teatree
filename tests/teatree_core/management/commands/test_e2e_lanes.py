"""The ``e2e lanes`` verb: derive ``{lane: [spec, ...]}`` from overlay seams (#3329)."""

import json
from io import StringIO
from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _e2e_lanes as _lanes
from teatree.core.management.commands._e2e_lanes import LaneOptions, run_lanes
from teatree.core.overlay import OverlayE2E
from tests.teatree_core.conftest import CommandOverlay

_SPECS = (
    "e2e/specs/login.spec.ts",
    "e2e/specs/checkout.spec.ts",
    "e2e/specs/orphan.spec.ts",
)


class _LanesE2E(OverlayE2E):
    def spec_paths(self) -> tuple[str, ...]:
        return _SPECS

    def run_provenance(self, spec_path: str) -> str:
        if "login" in spec_path:
            return "smoke"
        if "checkout" in spec_path:
            return "smoke"
        return ""  # orphan spec has no recorded lane


class _LanesOverlay(CommandOverlay):
    e2e = _LanesE2E()


_MOCK_OVERLAY = {"test": _LanesOverlay()}


class TestLaneSplit(TestCase):
    def test_groups_specs_by_provenance_lane_sorted(self) -> None:
        split = _lanes.lane_split(_LanesOverlay())
        assert split == {
            "smoke": ["e2e/specs/checkout.spec.ts", "e2e/specs/login.spec.ts"],
            _lanes.UNASSIGNED_LANE: ["e2e/specs/orphan.spec.ts"],
        }

    def test_no_specs_yields_empty_split(self) -> None:
        assert _lanes.lane_split(CommandOverlay()) == {}


class TestRunLanes(TestCase):
    @staticmethod
    def _run(options: _lanes.LaneOptions) -> tuple[dict[str, list[str]], str, str]:
        out, err = StringIO(), StringIO()
        split = _lanes.run_lanes(options, overlay=_LanesOverlay(), out=out, err=err)
        return split, out.getvalue(), err.getvalue()

    def test_json_emits_the_matrix_object_on_stdout(self) -> None:
        split, out, err = self._run(_lanes.LaneOptions(as_json=True))
        assert json.loads(out) == split
        assert split["smoke"] == ["e2e/specs/checkout.spec.ts", "e2e/specs/login.spec.ts"]
        assert err == ""

    def test_names_emits_every_spec_one_per_line_on_stdout(self) -> None:
        """``--names`` is a second MACHINE format (a shell loop reads it) — never stderr."""
        _, out, err = self._run(_lanes.LaneOptions(names=True))
        assert set(out.splitlines()) == set(_SPECS)
        assert err == ""

    def test_human_view_goes_to_stderr_leaving_stdout_clean(self) -> None:
        _, out, err = self._run(_lanes.LaneOptions())
        assert out == ""
        assert "smoke: e2e/specs/checkout.spec.ts, e2e/specs/login.spec.ts" in err

    def test_lane_filter_restricts_to_one_lane(self) -> None:
        split, _, _ = self._run(_lanes.LaneOptions(as_json=True, lane="smoke"))
        assert set(split) == {"smoke"}


class TestLanesCommand(TestCase):
    def test_command_returns_the_split(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, list[str]]", call_command("e2e", "lanes", json_output=True))
        assert result["smoke"] == ["e2e/specs/checkout.spec.ts", "e2e/specs/login.spec.ts"]


class TestRunLanesDirectImport(TestCase):
    """The seam is importable by name, not only through the module alias."""

    def test_returns_the_same_split_as_lane_split(self) -> None:
        out, err = StringIO(), StringIO()
        assert run_lanes(LaneOptions(), overlay=_LanesOverlay(), out=out, err=err) == _lanes.lane_split(_LanesOverlay())
