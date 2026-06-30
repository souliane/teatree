"""The raw-pid-kill sibling: dual identity, re-export reachability, cold import.

The raw-pid-kill deny gate was extracted whole out of ``hook_router`` into
``hooks/scripts/raw_pid_kill_guard.py`` (the #2384 Wave-2 router split, PR5).
These tests pin the EXTRACTION contract — the behavioural gate tests (which
commands deny vs allow, the deny message, the safe-kill guidance) live in
``tests/teatree_hooks/test_safe_kill_pretool_gate.py`` and exercise the handler
via the router re-export unchanged.

The contract:

* the sibling registers BOTH its bare ``raw_pid_kill_guard`` and dotted
    ``hooks.scripts.raw_pid_kill_guard`` identities as ONE module object, so a
    test patching a helper here and the handler the router invokes are the same;
* the router re-exports ``handle_block_raw_pid_kill`` under its original name, so
    ``router.handle_block_raw_pid_kill`` resolves to the SAME object the sibling
    defines (and ``_HANDLERS['PreToolUse']`` registers it unchanged);
* the sibling cold-imports with stdlib + the already-extracted ``managed_repo``
    sibling only — no Django, no ``teatree.core`` at module top (the live
    PreToolUse hook is a bare ``python3`` subprocess with no Django configured);
    the deny path's ``_fail_open_or_deny`` chokepoint stays in the router and is
    back-imported lazily inside the handler body.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.raw_pid_kill_guard as rpk

_SCRIPTS_DIR = Path(router.__file__).resolve().parent


class TestDualIdentity:
    def test_bare_and_dotted_names_are_one_module(self) -> None:
        assert sys.modules["raw_pid_kill_guard"] is sys.modules["hooks.scripts.raw_pid_kill_guard"]
        assert sys.modules["raw_pid_kill_guard"] is rpk


class TestRouterReExportReachable:
    """The handler is reachable via the sibling AND the router re-export — one object."""

    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_block_raw_pid_kill is rpk.handle_block_raw_pid_kill

    def test_patching_sibling_helper_is_seen_through_the_router(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patching a sibling internal affects the handler the router drives — one module object."""

        def _raise() -> None:
            raise RuntimeError

        monkeypatch.setattr(rpk, "_teatree_src_on_path", _raise)
        event = {"session_id": "sib", "tool_name": "Bash", "tool_input": {"command": "kill 4242"}}
        # With the src bootstrap patched to raise, the router-driven handler fails
        # OPEN (returns False) on the command it would otherwise deny — proving it
        # reads the sibling's globals.
        assert router.handle_block_raw_pid_kill(event) is False


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import raw_pid_kill_guard as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s.handle_block_raw_pid_kill({'tool_name': 'Edit', 'tool_input': {}}))"
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
