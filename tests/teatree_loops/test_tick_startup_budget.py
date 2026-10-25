"""The cold worker tick loads loop code without loading an agent SDK."""

import json
import subprocess
import sys

import pytest

_FORBIDDEN = ("claude_agent_sdk", "mcp", "openai", "pydantic_ai", "litellm", "anthropic")
# Between the lean tick path (1951 modules here) and the same path with the agent SDKs loaded (2327).
_MAX_TICK_IMPORT_MODULES = 2150
_PROBE = """
import json, sys
from teatree.utils.django_bootstrap import ensure_django
ensure_django()
import teatree.core.management.commands.loops_tick
import teatree.loop.tick
import teatree.loops.loop_table
roots = {roots!r}
print(json.dumps({{
    "count": len(sys.modules),
    "loaded": [root for root in roots if root in sys.modules],
    "limits_loaded": "teatree.llm.anthropic_limits" in sys.modules,
}}))
"""


def _tick_import_graph() -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(roots=_FORBIDDEN)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode:
        pytest.fail(f"cold tick import failed:\n{completed.stderr[-4000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.timeout(300)
def test_cold_tick_import_keeps_agent_sdks_out() -> None:
    graph = _tick_import_graph()

    assert graph["limits_loaded"] is True
    assert graph["loaded"] == []
    assert int(graph["count"]) <= _MAX_TICK_IMPORT_MODULES
