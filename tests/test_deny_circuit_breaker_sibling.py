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


class TestLeakGateNeverGranted:
    """#3252 — the PUBLIC-egress leak deny is never suppressible by a confirmed-FP grant.

    The banned-terms / quote-scanner leak path is fail-CLOSED always. Even with a
    ``[fp-confirmed:]`` token on the call, the breaker keeps denying it — while a
    non-leak deny carrying the same token is suppressed.
    """

    def _ctx(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)  # module constant, resolved at import
        monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")
        monkeypatch.setattr(
            router,
            "_CURRENT_DATA",
            {"session_id": "leak", "tool_name": "Bash", "tool_input": {"command": command}},
        )

    def test_leak_deny_with_fp_confirmed_token_still_denies(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ctx(monkeypatch, tmp_path, "glab mr note 1 -m body [fp-confirmed: not a leak]")
        reason = "BLOCKED: banned-terms posting gate (#1415). The body carries the banned term 'acme'."
        decision = dcb.apply_deny_circuit_breaker(reason)
        assert decision.allow is False, "a leak deny is never grantable, token or not"

    def test_non_leak_deny_with_fp_confirmed_token_is_suppressed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ctx(monkeypatch, tmp_path, "uv run pytest --no-cov -q [fp-confirmed: known quick]")
        decision = dcb.apply_deny_circuit_breaker("BLOCKED: the orchestrator ran a heavy command.")
        assert decision.allow is True, "a confirmed non-leak FP is suppressed"


class TestCallSignatureDiscrimination:
    """#3252 — the call signature distinguishes commands and folds out escape tokens."""

    def test_bash_signature_strips_fp_confirmed_token(self) -> None:
        tokened = dcb._call_signature(
            {"tool_name": "Bash", "tool_input": {"command": "uv run pytest -q [fp-confirmed: known]"}}
        )
        plain = dcb._call_signature({"tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}})
        assert tokened == plain, "an escape token must not split a command's signature"

    def test_bash_signature_strips_allow_banned_term_prefix(self) -> None:
        prefixed = dcb._call_signature(
            {"tool_name": "Bash", "tool_input": {"command": "ALLOW_BANNED_TERM=1 git commit -m x"}}
        )
        plain = dcb._call_signature({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}})
        assert prefixed == plain

    def test_distinct_commands_have_distinct_signatures(self) -> None:
        a = dcb._call_signature({"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m first"}})
        b = dcb._call_signature({"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m second"}})
        assert a != b

    def test_edit_signature_keys_on_file_path(self) -> None:
        sig = dcb._call_signature({"tool_name": "Edit", "tool_input": {"file_path": "/repo/src/a.py"}})
        assert sig == "/repo/src/a.py"

    def test_non_bash_edit_tool_signature_is_tool_name(self) -> None:
        assert dcb._call_signature({"tool_name": "Task", "tool_input": {"prompt": "x"}}) == "task"

    def test_malformed_data_yields_empty_signature(self) -> None:
        assert dcb._call_signature({"tool_name": "Bash", "tool_input": None}) == ""


class TestFpConfirmedTokenSurfaces:
    """#3252 — the ``[fp-confirmed:]`` token is honoured across the token fields."""

    def test_token_in_edit_new_string_is_confirmed(self) -> None:
        assert dcb._fp_confirmed({"tool_name": "Edit", "tool_input": {"new_string": "x [fp-confirmed: fine]"}}) is True

    def test_empty_reason_token_does_not_confirm(self) -> None:
        assert dcb._fp_confirmed({"tool_name": "Bash", "tool_input": {"command": "x [fp-confirmed: ]"}}) is False

    def test_no_token_is_not_confirmed(self) -> None:
        assert dcb._fp_confirmed({"tool_name": "Bash", "tool_input": {"command": "git status"}}) is False


class TestGrantStoreEdgeCases:
    """#3252 — the grant store is crash-proof and idempotent."""

    def test_empty_session_never_grants(self) -> None:
        assert dcb._fp_grant_exists("", "fp") is False
        dcb._record_fp_grant("", "fp")  # no-op, must not raise

    def test_record_is_idempotent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)  # module constant, resolved at import
        dcb._record_fp_grant("s", "fp-abc")
        dcb._record_fp_grant("s", "fp-abc")
        grants = (tmp_path / "s.fp-grants").read_text(encoding="utf-8").splitlines()
        assert grants.count("fp-abc") == 1, "a repeated grant is deduped, not appended twice"
        assert dcb._fp_grant_exists("s", "fp-abc") is True

    def test_prerecorded_grant_suppresses_matching_deny(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A grant recorded earlier suppresses the identical FP without re-prompting."""
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        data = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "npm run build"}}
        reason = "BLOCKED: the orchestrator ran a heavy command: `npm run build`."
        monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")
        monkeypatch.setattr(router, "_CURRENT_DATA", data)
        fingerprint = dcb._deny_fingerprint(dcb._deny_gate_id(reason), reason, dcb._call_signature(data))
        dcb._record_fp_grant("s", fingerprint)
        assert dcb.apply_deny_circuit_breaker(reason).allow is True


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
