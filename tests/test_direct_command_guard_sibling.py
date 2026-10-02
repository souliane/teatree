"""The direct-command sibling: dual identity, re-export reachability, cold import.

The block-direct-commands deny gate was extracted whole out of ``hook_router``
into ``hooks/scripts/direct_command_guard.py`` (the #2384 Wave-2 router split,
PR7). These tests pin the EXTRACTION contract — the behavioural gate tests (which
commands deny vs allow, the prefix allowlist, the F3/F6/F8 carve-outs) live in
``tests/test_bash_command_blocker.py``, ``tests/test_hook_router_gate_bypass_class.py``,
and ``tests/test_lockout_regression_corpus.py`` and exercise the handler via the
router re-export unchanged.

The contract:

* the sibling registers BOTH its bare ``direct_command_guard`` and dotted
    ``hooks.scripts.direct_command_guard`` identities as ONE module object, so a
    test patching a helper here and the handler the router invokes are the same;
* the router re-exports ``handle_block_direct_commands`` (and the ``deny_match``
    helper the denylist tests read as ``router._deny_match`` + the
    ``BLOCKED_COMMANDS`` aggregation the BLUEPRINT/skills/merge-execution prose
    cites as ``hook_router._BLOCKED_COMMANDS``) under their original router names,
    so ``router.handle_block_direct_commands`` resolves to the SAME object the
    sibling defines (and ``_HANDLERS['PreToolUse']`` registers it unchanged);
* the deny chokepoint ``emit_pretooluse_deny`` the gate SHARES with every other
    PreToolUse deny gate stays defined ONLY in the router — the sibling
    back-imports it lazily, so the ``_write_pretooluse_deny`` writer and the
    repeated-denial circuit breaker stay in the router (the never-lockout
    contract, #2349);
* the sibling cold-imports with stdlib only — no Django, no ``teatree`` at module
    top (the live PreToolUse hook is a bare ``python3`` subprocess with no Django
    configured).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.direct_command_guard as dcg
import hooks.scripts.hook_router as router

_SCRIPTS_DIR = Path(router.__file__).resolve().parent


def _bash_event(command: str) -> dict:
    return {"session_id": "sib", "tool_name": "Bash", "tool_input": {"command": command}}


class TestCanonicalIdentity:
    def test_module_has_one_canonical_package_identity(self) -> None:
        # The package-relative refactor gives the sibling a SINGLE canonical
        # identity (``hooks.scripts.direct_command_guard``) — the old bare-name alias is gone —
        # so the module a test patches is the one the router imports.
        assert sys.modules["hooks.scripts.direct_command_guard"] is dcg


class TestRouterReExportReachable:
    """Handler, helper, and the moved aggregation resolve to ONE object across the sibling and the router."""

    def test_handler_reexport_is_the_same_object(self) -> None:
        assert router.handle_block_direct_commands is dcg.handle_block_direct_commands

    def test_helper_reexport_is_the_same_object(self) -> None:
        assert router._deny_match is dcg.deny_match

    def test_blocked_commands_reexport_is_the_same_object(self) -> None:
        assert router._BLOCKED_COMMANDS is dcg.BLOCKED_COMMANDS

    def test_deny_chokepoint_has_one_definition_in_the_router(self) -> None:
        """The shared deny writer ``emit_pretooluse_deny`` stays defined only in the router, not the sibling."""
        assert hasattr(router, "emit_pretooluse_deny")
        assert not hasattr(dcg, "emit_pretooluse_deny")

    def test_router_handler_denies_a_real_bypass(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-vacuous: the router-driven handler denies a t3-CLI-bypass via the sibling + lazy back-import."""
        assert router.handle_block_direct_commands(_bash_event("python manage.py runserver")) is True
        deny = json.loads(capsys.readouterr().out.strip())
        assert deny["permissionDecision"] == "deny"

    def test_patching_sibling_helper_is_seen_through_the_router(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patching the sibling's denylist matcher affects the handler the router drives — one module object."""
        monkeypatch.setattr(dcg, "deny_match", lambda _command: None)
        assert router.handle_block_direct_commands(_bash_event("python manage.py runserver")) is not True


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import direct_command_guard as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s.handle_block_direct_commands({'tool_name': 'Edit', 'tool_input': {}}))"
                ),
                str(_SCRIPTS_DIR),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"
