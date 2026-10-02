"""Per-loop connector preflight — scope the gate to ONE loop's overlay (LOOP-PR-C).

The fleet-wide ``run_connector_preflight`` probes every overlay; a per-loop tick
must not. ``run_loop_connector_preflight`` narrows the gate to the loop's own
overlay and only fires when the loop is enabled + due, so an unrelated overlay's
outage can never ``SystemExit`` an unrelated loop's tick — while the loop's OWN
down connector still fails loud.
"""

import datetime as dt
import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Loop, Worktree
from teatree.core.overlay import OverlayBase, OverlayConnectors, ProvisionStep
from teatree.loops.connector_preflight import run_loop_connector_preflight


class _CleanOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


class _SlackDownOverlayConnectors(OverlayConnectors):
    def preflight(self) -> list:
        def _probe() -> None:
            msg = "Slack auth.test failed: missing_scope"
            raise RuntimeError(msg)

        return [_probe]


class _SlackDownOverlay(OverlayBase):
    connectors = _SlackDownOverlayConnectors()

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


@contextmanager
def _registered(overlays: dict[str, OverlayBase]) -> Iterator[None]:
    with (
        patch("teatree.core.connector_preflight.get_all_overlays", return_value=overlays),
        patch("teatree.loops.connector_preflight.get_all_overlays", return_value=overlays),
        patch("teatree.core.overlay_loader.OverlayConfigResolver.all_names", return_value=list(overlays)),
    ):
        yield


def _seed(
    name: str, overlay: str, *, enabled: bool = True, delay_seconds: int = 60, last_run_at: dt.datetime | None = None
) -> None:
    Loop.objects.create(
        name=name,
        script=f"src/teatree/loops/{name}/loop.py",
        delay_seconds=delay_seconds,
        overlay=overlay,
        enabled=enabled,
        last_run_at=last_run_at,
    )


class TestRunLoopConnectorPreflight(TestCase):
    def test_clean_own_overlay_returns_none(self) -> None:
        _seed("probe-alpha", overlay="alpha")
        with _registered({"alpha": _CleanOverlay(), "beta": _SlackDownOverlay()}):
            assert run_loop_connector_preflight("probe-alpha") is None

    def test_unrelated_down_overlay_does_not_systemexit(self) -> None:
        _seed("probe-alpha", overlay="alpha")
        with _registered({"alpha": _CleanOverlay(), "beta": _SlackDownOverlay()}):
            assert run_loop_connector_preflight("probe-alpha") is None

    def test_own_down_overlay_systemexits(self) -> None:
        _seed("probe-beta", overlay="beta")
        with _registered({"alpha": _CleanOverlay(), "beta": _SlackDownOverlay()}), pytest.raises(SystemExit) as excinfo:
            run_loop_connector_preflight("probe-beta")
        assert excinfo.value.code != 0
        assert "beta" in str(excinfo.value)

    def test_disabled_loop_skips_its_own_down_overlay(self) -> None:
        _seed("probe-beta", overlay="beta", enabled=False)
        with _registered({"beta": _SlackDownOverlay()}):
            assert run_loop_connector_preflight("probe-beta") is None

    def test_cooling_loop_not_due_skips_its_own_down_overlay(self) -> None:
        _seed("probe-beta", overlay="beta", delay_seconds=3600, last_run_at=timezone.now())
        with _registered({"beta": _SlackDownOverlay()}):
            assert run_loop_connector_preflight("probe-beta") is None

    def test_missing_loop_row_is_a_noop(self) -> None:
        with _registered({"beta": _SlackDownOverlay()}):
            assert run_loop_connector_preflight("does-not-exist") is None

    def test_blank_overlay_single_install_preflights_the_one_overlay(self) -> None:
        _seed("probe-beta", overlay="")
        with _registered({"beta": _SlackDownOverlay()}), pytest.raises(SystemExit):
            run_loop_connector_preflight("probe-beta")

    def test_blank_overlay_multi_install_skips_when_ambient_unset(self) -> None:
        _seed("probe-beta", overlay="")
        with (
            _registered({"alpha": _CleanOverlay(), "beta": _SlackDownOverlay()}),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("T3_OVERLAY_NAME", None)
            assert run_loop_connector_preflight("probe-beta") is None

    def test_unknown_overlay_name_skips_rather_than_probing_the_fleet(self) -> None:
        _seed("probe-beta", overlay="ghost")
        with _registered({"alpha": _CleanOverlay(), "beta": _SlackDownOverlay()}):
            assert run_loop_connector_preflight("probe-beta") is None
