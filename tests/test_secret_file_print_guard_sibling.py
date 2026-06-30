"""The secret-file-print sibling: dual identity, re-export reachability, cold import.

The secret-file-print deny gate was extracted whole out of ``hook_router`` into
``hooks/scripts/secret_file_print_guard.py`` (the #2384 Wave-2 router split, PR4).
These tests pin the EXTRACTION contract — the behavioural gate tests (which
commands deny vs allow, the deny message, the re-emitter-pipe edge cases) live in
``test_block_secret_file_print_hook.py`` and exercise the handler via the router
re-export unchanged.

The contract:

* the sibling registers BOTH its bare ``secret_file_print_guard`` and dotted
    ``hooks.scripts.secret_file_print_guard`` identities as ONE module object, so a
    test patching a helper here and the handler the router invokes are the same;
* the router re-exports ``handle_block_secret_file_print`` under its original
    name, so ``router.handle_block_secret_file_print`` resolves to the SAME object
    the sibling defines (and ``_HANDLERS['PreToolUse']`` registers it unchanged);
* the sibling cold-imports with stdlib only — no Django, no ``teatree.core`` at
    module top (the live PreToolUse hook is a bare ``python3`` subprocess with no
    Django configured); the deny path's ``_fail_open_or_deny`` chokepoint stays in
    the router and is back-imported lazily inside the handler body.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.secret_file_print_guard as sfp

_SCRIPTS_DIR = Path(router.__file__).resolve().parent


class TestDualIdentity:
    def test_bare_and_dotted_names_are_one_module(self) -> None:
        assert sys.modules["secret_file_print_guard"] is sys.modules["hooks.scripts.secret_file_print_guard"]
        assert sys.modules["secret_file_print_guard"] is sfp


class TestRouterReExportReachable:
    """The handler is reachable via the sibling AND the router re-export — one object."""

    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_block_secret_file_print is sfp.handle_block_secret_file_print

    def test_patching_sibling_helper_is_seen_through_the_router(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patching a sibling internal affects the handler the router drives — one module object."""
        monkeypatch.setattr(sfp, "_is_secret_print", lambda _command: False)
        event = {"session_id": "sib", "tool_name": "Bash", "tool_input": {"command": "cat ~/.teatree.toml"}}
        # With _is_secret_print patched to False, the router-driven handler allows the
        # command it would otherwise deny — proving it reads the sibling's globals.
        assert router.handle_block_secret_file_print(event) is False


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import secret_file_print_guard as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s._is_secret_print('cat ~/.teatree.toml'))"
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
        assert result.stdout.strip() == "True"
