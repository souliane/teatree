"""``t3 <overlay> signals`` — the CLI-surface wire test whose absence let #13 ship green.

SIG-PR-1 advertised a ``t3 <overlay> signals`` command but never registered the
leaf on the overlay CLI, so the command did not exist; the only tests called
``compute_factory_signals()`` directly, so CI stayed green over a dead surface.
This exercises the REAL built overlay Typer app end to end: the ``signals`` leaf
resolves, forwards ``--json`` / ``--window-days`` verbatim to the core
management command, and the ``T3_OVERLAY_NAME`` the bridge sets flows through to
the report's ``overlay`` scope field (#25).

The ``managepy_core`` subprocess boundary is the one unstoppable external the
test-doctrine permits stubbing: it is routed in-process through ``call_command``
against the test DB (mirroring ``tests/test_cli_wip.py``), so the leaf's arg
forwarding and the command's JSON contract are both real while the subprocess
bootstrap is proven elsewhere. The scope-propagation-through-a-real-subprocess
path is additionally covered by ``tests/conformance/test_signals_scope_parity.py``.
"""

import json
import os

import pytest
import typer
from django.core.management import call_command
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder

runner = CliRunner()

_OVERLAY = "test-overlay"


def _in_process_managepy_core(*args: str, overlay_name: str = "") -> None:
    """In-process stand-in for the ``signals`` subprocess seam.

    The real leaf delegates to ``python -m teatree signals …`` and streams the
    child's stdout up; the child sets ``T3_OVERLAY_NAME`` on its env and
    django-typer prints the ``handle`` return. Here that whole boundary collapses
    to a ``call_command`` against the test DB with ``T3_OVERLAY_NAME`` set —
    django-typer prints the ``handle`` return to the ``CliRunner``-captured
    stdout, the same document a front-end would parse.
    """
    prior = os.environ.get("T3_OVERLAY_NAME")
    if overlay_name:
        os.environ["T3_OVERLAY_NAME"] = overlay_name
    try:
        call_command(*args)
    finally:
        if prior is None:
            os.environ.pop("T3_OVERLAY_NAME", None)
        else:
            os.environ["T3_OVERLAY_NAME"] = prior


@pytest.fixture(autouse=True)
def _route_bridge_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.cli.overlay.managepy_core", _in_process_managepy_core)


def _app() -> typer.Typer:
    return OverlayAppBuilder(_OVERLAY, None).build()


class TestSignalsCliSurface(TestCase):
    def test_signals_leaf_is_registered_and_emits_json(self) -> None:
        result = runner.invoke(_app(), ["signals", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)  # raises if the human view leaked onto stdout
        assert {"overlay", "window_days", "verdict", "signals"} <= set(payload)
        assert len(payload["signals"]) == 5

    def test_overlay_scope_flows_through_the_bridge(self) -> None:
        # T3_OVERLAY_NAME the leaf sets must reach the report's scope field — the
        # #25 CLI-scope half of the CLI/MCP parity contract.
        result = runner.invoke(_app(), ["signals", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["overlay"] == _OVERLAY

    def test_window_days_is_forwarded_verbatim(self) -> None:
        result = runner.invoke(_app(), ["signals", "--json", "--window-days", "7"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["window_days"] == 7

    def test_default_human_view_renders_the_scoped_markdown_header(self) -> None:
        result = runner.invoke(_app(), ["signals"])
        assert result.exit_code == 0, result.output
        assert f"scope: {_OVERLAY}" in result.stdout
