"""The deny-circuit-breaker sibling: single canonical identity, re-export reachability, cold import.

The repeated-denial circuit breaker was extracted whole out of ``hook_router``
into ``hooks/scripts/deny_circuit_breaker.py`` (the #2384 Wave-2 router split,
PR3). These tests pin the extraction contract — the behavioural breaker tests
(streak counting, UX-relax vs safety-escalate, kill-switch) live in
``test_hook_router_deny_circuit_breaker.py`` and exercise the real subprocess
chain unchanged.

The contract:

* the sibling has a SINGLE canonical package identity ``hooks.scripts.deny_circuit_breaker``
    (no bare-name alias after the package-relative refactor), so a test patching a
    helper here and the breaker the router invokes are the same object;
* the router re-exports the breaker entry points under their original underscore
    names, so ``_apply_deny_circuit_breaker`` / ``_reset_deny_streak`` /
    ``_deny_circuit_breaker_threshold`` / ``_deny_circuit_breaker_enabled`` /
    ``_deny_is_ux_gate`` resolve via the router exactly as before the move;
* the sibling cold-imports with stdlib + already-extracted siblings only — no
    Django, no ``teatree.core`` at module top (the live PreToolUse hook is a bare
    ``python3`` subprocess with no Django configured).
"""

import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.deny_circuit_breaker as dcb
import hooks.scripts.hook_router as router

_SCRIPTS_DIR = Path(router.__file__).resolve().parent


class TestCanonicalIdentity:
    def test_module_has_one_canonical_package_identity(self) -> None:
        # The package-relative refactor gives the sibling a SINGLE canonical
        # identity (``hooks.scripts.deny_circuit_breaker``) — the old bare-name alias is gone —
        # so the module a test patches is the one the router imports.
        assert sys.modules["hooks.scripts.deny_circuit_breaker"] is dcb


class TestRouterReExportReachable:
    """The breaker entry points are reachable via the sibling AND the router re-export."""

    def test_reexports_are_the_same_objects(self) -> None:
        assert router._apply_deny_circuit_breaker is dcb.apply_deny_circuit_breaker
        assert router._reset_deny_streak is dcb.reset_deny_streak
        assert router._deny_circuit_breaker_threshold is dcb.deny_circuit_breaker_threshold
        assert router._deny_circuit_breaker_enabled is dcb.deny_circuit_breaker_enabled
        assert router._deny_is_ux_gate is dcb.deny_is_ux_gate

    def test_patching_sibling_helper_is_seen_through_the_router(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patching a sibling internal affects the breaker the router drives — one module object."""
        monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")
        monkeypatch.setattr(router, "_CURRENT_DATA", {"session_id": "sib"})
        monkeypatch.setattr(dcb, "deny_circuit_breaker_enabled", lambda: False)
        # Disabled breaker is a pure pass-through: the original deny stands.
        decision = router._apply_deny_circuit_breaker("LOOP REGISTRATION: x")
        assert decision.allow is False
        assert decision.reason == "LOOP REGISTRATION: x"


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import deny_circuit_breaker as d; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(d.deny_is_ux_gate('SKILL LOADING ENFORCEMENT: x'))"
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
