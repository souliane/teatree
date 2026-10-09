"""A hook handler that CRASHES is cannot-evaluate, never a content deny.

A PreToolUse handler signals a content deny by returning ``True`` (the router
translates that into ``sys.exit(2)``, the only exit code Claude Code honours as
a block). A handler that *raises* — because its environment is broken, a config
is malformed, or its own internal fail-open is incomplete — must be treated as
cannot-evaluate: the broken gate is skipped and the chain continues. A crash
must NEVER manifest as exit 2, and one broken gate must NEVER disable the gates
that run after it.

Integration-style: the real ``hook_router.main()`` runs in a subprocess so the
real exit code propagates through ``sys.exit``. A crashing handler — and, in
the second case, a real downstream deny handler — are spliced into the live
``_HANDLERS`` registry of the imported module, then ``main()`` is driven with a
real stdin payload exactly as the harness invokes it.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"

_DRIVER = """
import io, sys, json
import hooks.scripts.hook_router as r
{splice}
sys.argv = ["hook_router.py", "--event", "PreToolUse"]
sys.stdin = io.StringIO(json.dumps({payload}))
r.main()
"""


@pytest.fixture
def home() -> Iterator[dict[str, str]]:
    """A clean HOME so the orchestrator-Bash gate reads its default (enabled)."""
    with tempfile.TemporaryDirectory() as tmp:
        yield {**os.environ, "HOME": tmp, "USERPROFILE": tmp}


def _drive_router(env: dict[str, str], *, splice: str, payload: dict) -> subprocess.CompletedProcess[str]:
    code = _DRIVER.format(splice=splice, payload=json.dumps(payload))
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**env, "PYTHONPATH": str(HOOK_ROUTER.parent.parent.parent)},
    )


def test_crashing_handler_does_not_exit_2(home: dict[str, str]) -> None:
    """A handler raising mid-chain must not be read as a deny (exit 2)."""
    splice = (
        "def _boom(data):\n    raise RuntimeError('broken gate environment')\nr._HANDLERS['PreToolUse'] = [_boom]\n"
    )
    result = _drive_router(home, splice=splice, payload={"tool_name": "Bash", "tool_input": {"command": "git status"}})

    assert result.returncode != 2, f"a handler crash must not surface as a deny; stderr={result.stderr!r}"
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_crash_does_not_disable_downstream_deny_gate(home: dict[str, str]) -> None:
    """A crash in one gate must not suppress a real deny from a later gate."""
    splice = (
        "def _boom(data):\n"
        "    raise RuntimeError('broken gate environment')\n"
        "def _deny(data):\n"
        "    return r.emit_pretooluse_deny('downstream gate denies')\n"
        "r._HANDLERS['PreToolUse'] = [_boom, _deny]\n"
    )
    result = _drive_router(home, splice=splice, payload={"tool_name": "Bash", "tool_input": {"command": "git status"}})

    assert result.returncode == 2, f"downstream deny must still fire after an upstream crash; stderr={result.stderr!r}"
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "downstream gate denies"
