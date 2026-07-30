"""Umbrella guard: no teatree RUNTIME code path depends on a claude.ai connector.

The single grep-able definition of done for the runtime connector-decoupling.
Product/runtime modules must reach every platform via a direct official API, not
by punting to an ``mcp__claude_ai_*`` connector. ``hooks/`` and ``eval/`` are
excluded — they legitimately *forbid* raw MCP-Slack sends (they name the tokens
to gate/detect them, not to call them).
"""

from pathlib import Path

from django.test import TestCase

import teatree
from teatree.core.connector_manifest import overlay_connector_manifests

_SRC_ROOT = Path(teatree.__file__).resolve().parent
_EXCLUDED_DIRS = ("hooks", "eval")
_CONNECTOR_PUNT_STRINGS = ("requires Claude MCP", "mcp__claude_ai")


def _runtime_modules() -> list[Path]:
    return [
        path
        for path in _SRC_ROOT.rglob("*.py")
        if not any(part in _EXCLUDED_DIRS for part in path.relative_to(_SRC_ROOT).parts)
    ]


class TestNoClaudeAiConnectorRuntimeDependency(TestCase):
    def test_no_module_punts_to_a_claude_ai_connector(self) -> None:
        offenders = {
            str(path.relative_to(_SRC_ROOT)): needle
            for path in _runtime_modules()
            for needle in _CONNECTOR_PUNT_STRINGS
            if needle in path.read_text(encoding="utf-8")
        }
        assert offenders == {}, f"runtime modules punt to a claude.ai connector: {offenders}"

    def test_no_registered_overlay_declares_a_required_claude_ai_connector(self) -> None:
        required = [
            (manifest.overlay, req.name)
            for manifest in overlay_connector_manifests()
            for req in manifest.requirements
            if req.required
        ]
        assert required == [], (
            f"a registered overlay declares REQUIRED connectors {required}; preflight could "
            "SystemExit the loop on a claude.ai connector being down"
        )
