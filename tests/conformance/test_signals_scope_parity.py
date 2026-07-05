"""signals CLI <-> MCP scope parity — the named guard that replaced "can never drift" (SIG-4, #25).

The MCP ``factory_signals`` docstring once *claimed* the CLI and MCP surfaces "can
never drift" — an unenforced guarantee. In fact they silently reported different
scopes: the CLI is env-scoped to ``T3_OVERLAY_NAME`` while the MCP tool defaults
global, and neither stamped the scope anywhere in the output, so a consumer could
not tell an overlay-scoped reading from a global one. #25 added the ``overlay``
scope field to both; this lane is the parity test the docstring now points at
instead of the empty promise.

Both surfaces run the SAME ``compute_factory_signals`` path, so the guard is a
schema + scope-value comparison: the CLI ``signals --json`` for ``T3_OVERLAY_NAME=X``
and ``mcp.search.factory_signals(overlay=X)`` must be schema-identical INCLUDING
the ``overlay`` field, and a global reading must be distinguishable from a scoped
one from the output alone.
"""

import json
import os
from typing import Any
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from teatree.mcp.search import factory_signals as mcp_factory_signals

_OVERLAY = "t3-teatree"


def _cli_json(overlay: str) -> dict[str, Any]:
    # The CLI reads its scope from T3_OVERLAY_NAME (empty => global), exactly as
    # the `t3 <overlay> signals` bridge sets it. `call_command` returns the same
    # JSON document django-typer prints to stdout for a front-end.
    with mock.patch.dict(os.environ, {"T3_OVERLAY_NAME": overlay}):
        return json.loads(call_command("signals", "--json"))


def _schema(payload: dict[str, Any]) -> Any:
    # Structural fingerprint that ignores the wall-clock `generated_at` and every
    # value except the scope: the key sets at both levels plus the scope string.
    top = {key for key in payload if key != "generated_at"}
    rows = tuple(sorted(sorted(row) for row in payload["signals"]))
    return top, rows, payload["overlay"]


class TestSignalsScopeParity(TestCase):
    def test_cli_and_mcp_are_schema_identical_including_scope(self) -> None:
        cli = _cli_json(_OVERLAY)
        mcp = mcp_factory_signals(overlay=_OVERLAY)
        assert _schema(cli) == _schema(mcp)
        assert cli["overlay"] == mcp["overlay"] == _OVERLAY

    def test_global_reading_is_distinguishable_from_scoped(self) -> None:
        global_cli = _cli_json("")
        scoped_cli = _cli_json(_OVERLAY)
        assert global_cli["overlay"] == ""
        assert scoped_cli["overlay"] == _OVERLAY
        assert global_cli["overlay"] != scoped_cli["overlay"]

    def test_mcp_omitted_overlay_is_the_global_scope(self) -> None:
        # The documented omit-means-global default is observable in the payload,
        # not merely asserted in prose.
        assert mcp_factory_signals()["overlay"] == ""
        assert mcp_factory_signals(overlay=_OVERLAY)["overlay"] == _OVERLAY

    def test_both_surfaces_carry_the_scope_key_never_absent(self) -> None:
        # A consumer never has to guess the scope: the key is present on both
        # surfaces even for the global view.
        assert "overlay" in mcp_factory_signals()
        assert "overlay" in _cli_json("")
