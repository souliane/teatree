"""The protect-default-branch sibling: single canonical identity, re-export, cold import.

``handle_protect_default_branch`` was extracted out of ``hook_router`` (shrink-only
over its module-health cap) into ``hooks/scripts/protect_default_branch_guard.py`` to
offset the ``raw_ticket_ignore_loop_gate`` registration added in the same change —
see ``docs/module-health.md``'s extract-first rule. These tests pin the EXTRACTION
contract, the same shape ``test_raw_pid_kill_guard_sibling.py`` pins; the behavioural
gate tests (the scenario matrix) live unchanged in
``tests/test_protect_default_branch_target_eval.py``, driven through the router
re-export.
"""

import subprocess
import sys
from pathlib import Path

import hooks.scripts.hook_router as router
import hooks.scripts.protect_default_branch_guard as pdb_guard

_SCRIPTS_DIR = Path(router.__file__).resolve().parent


class TestCanonicalIdentity:
    def test_module_has_one_canonical_package_identity(self) -> None:
        assert sys.modules["hooks.scripts.protect_default_branch_guard"] is pdb_guard


class TestRouterReExportReachable:
    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_protect_default_branch is pdb_guard.handle_protect_default_branch

    def test_registered_in_pretooluse_chain(self) -> None:
        assert pdb_guard.handle_protect_default_branch in router._HANDLERS["PreToolUse"]

    def test_non_file_path_tool_allows(self) -> None:
        """A quick end-to-end sanity check through the re-export — no repo needed."""
        event = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        assert router.handle_protect_default_branch(event) is False


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import protect_default_branch_guard as s; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(s.handle_protect_default_branch({'tool_name': 'Bash', 'tool_input': {}}))"
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
