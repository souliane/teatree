"""The bounded-wait sibling: canonical identity, re-export reachability, cold import.

The behavioural gate tests (which commands deny vs allow, the bounded rewrite the
deny hands back) live in ``tests/teatree_hooks/test_unbounded_wait_pretool_gate.py``.
These pin the WIRING contract the router split requires of every sibling gate:

* one canonical package identity (``hooks.scripts.unbounded_wait_guard``), so a test
    patching a helper here reaches exactly what the router invokes;
* the router re-exports ``handle_block_unbounded_wait`` under its own name, so
    ``router.handle_block_unbounded_wait`` is the SAME object the sibling defines and
    ``_HANDLERS['PreToolUse']`` registers it unchanged;
* the sibling cold-imports with stdlib + the ``managed_repo`` sibling only — the live
    PreToolUse hook is a bare ``python3`` subprocess with no Django configured, so a
    Django/``teatree.core`` import at module top would break the gate on every call.
"""

import subprocess
import sys
from pathlib import Path

import hooks.scripts.hook_router as router
import hooks.scripts.unbounded_wait_guard as guard

_SCRIPTS_DIR = Path(router.__file__).resolve().parent


class TestCanonicalIdentity:
    def test_module_has_one_canonical_package_identity(self) -> None:
        assert sys.modules["hooks.scripts.unbounded_wait_guard"] is guard


class TestRouterReExportReachable:
    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_block_unbounded_wait is guard.handle_block_unbounded_wait

    def test_it_is_on_the_pretooluse_chain(self) -> None:
        assert guard.handle_block_unbounded_wait in router._HANDLERS["PreToolUse"]


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import unbounded_wait_guard as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s.handle_block_unbounded_wait({'tool_name': 'Edit', 'tool_input': {}}))"
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
