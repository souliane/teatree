"""Every leak-family deny is non-grantable, driven from the REAL message formatters (#4218).

The breaker's confirmed-false-positive escape (``[fp-confirmed: …]``) is a
session-scoped grant an agent writes itself. It must never reach the PUBLIC-egress
leak family — and the strongest member of that family, the high-confidence-secret
deny, named no gate at all, so it was classified as an ordinary grantable deny: an
agent that believed a live token was a placeholder could grant itself the right to
post it, and every identical call afterwards was suppressed silently.

The old guard could not catch that. It handed ``_deny_is_leak_gate`` a literal
reason string containing ``"banned-terms"``, so it proved the substring matcher
matches a substring it was given and nothing about the messages the system emits.
Every message here is the output of the real formatter or the real constant, and
:class:`TestLeakMessageSourceCoverage` fails when a new deny-message source appears
in a leak module without being classified.
"""

import subprocess
import types
from types import ModuleType

import pytest

import hooks.scripts.banned_terms.gate as banned_terms_gate
import hooks.scripts.deny_circuit_breaker as dcb
import hooks.scripts.hook_router as router
from teatree.hooks import ai_signature_gate, banned_terms_scanner, quote_gate_messages, quote_scanner

# An AWS-key-shaped literal that matches ``publish_surface._SECRET_PATTERNS`` while
# spelling out that it is a fixture. The detector keys on shape, so no real
# credential is ever needed to reach the credential deny.
_SYNTHETIC_SECRET = "AKIAEXAMPLEEXAMPLE00"

_FP_TOKEN = "[fp-confirmed: this is a fake token]"

_AI_SIG_COMMIT = 'git commit -m "feat: x\n\nCo-Authored-By: a <b@c>"'


def _stub_ai_sig_scanner(monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str = "") -> None:
    completed = subprocess.CompletedProcess([], 1, stdout, stderr)
    monkeypatch.setattr(router, "run_t3", lambda *_a, **_k: completed)


def _high_quote_result() -> quote_scanner.ScanResult:
    return quote_scanner.ScanResult(
        findings=[quote_scanner.Finding(name="attributed_quote", severity=quote_scanner.HIGH, excerpt="…")]
    )


# The leak family, keyed by the source that renders each message, valued by that
# message's RENDERED output and the gate id its emitter stamps. A member is registered
# by its rendered output, never by a literal, so rewording a message cannot leave a
# stale copy here passing while the live one falls out of the family.
_LEAK_DENY_MESSAGES: dict[str, tuple[str, str]] = {
    "banned_terms_scanner.format_block_message": (banned_terms_scanner.format_block_message("acme"), "banned_terms"),
    "banned_terms_scanner.format_unresolvable_body_message": (
        banned_terms_scanner.format_unresolvable_body_message(),
        "banned_terms",
    ),
    "banned_terms_scanner.format_unavailable_body_source_message": (
        banned_terms_scanner.format_unavailable_body_source_message(),
        "banned_terms",
    ),
    "banned_terms_scanner.format_scanner_unavailable_message": (
        banned_terms_scanner.format_scanner_unavailable_message(),
        "banned_terms",
    ),
    "banned_terms_scanner.format_scanner_timeout_message": (
        banned_terms_scanner.format_scanner_timeout_message(),
        "banned_terms",
    ),
    "banned_terms_scanner._STORE_UNREADABLE_DENY": (banned_terms_scanner._STORE_UNREADABLE_DENY, "banned_terms"),
    "banned_terms_scanner._TERMS_REQUIRED_UNSET_DENY": (
        banned_terms_scanner._TERMS_REQUIRED_UNSET_DENY,
        "banned_terms",
    ),
    "banned_terms_scanner.marker_deny_message": (
        banned_terms_scanner.marker_deny_message(banned_terms_scanner.UNRESOLVABLE_BODY_MARKER) or "",
        "banned_terms",
    ),
    "quote_gate_messages.format_block_message": (
        quote_gate_messages.format_block_message(_high_quote_result()),
        "quote_scanner",
    ),
    "quote_gate_messages.format_dispatch_block_message": (
        quote_gate_messages.format_dispatch_block_message(_high_quote_result()),
        "dispatch_quote_scanner",
    ),
    "quote_gate_messages.format_task_entry_block_message": (
        quote_gate_messages.format_task_entry_block_message(_high_quote_result()),
        "quote_scanner",
    ),
    "ai_signature_gate.format_block_message": (
        ai_signature_gate.format_block_message("AI-signature scan: 1 banned trailer(s)"),
        "ai_signature",
    ),
    "ai_signature_gate.format_scanner_error_message": (
        ai_signature_gate.format_scanner_error_message("Traceback"),
        "ai_signature",
    ),
    "gate._BANNED_TERMS_CREDENTIAL_DENY": (banned_terms_gate._BANNED_TERMS_CREDENTIAL_DENY, "banned_terms"),
}

# Sources in a leak module that render a WARNING, not a deny — they never reach the
# breaker, so grantability does not apply to them.
_WARN_ONLY_SOURCES: frozenset[str] = frozenset({"quote_gate_messages.format_warn_message"})

_LEAK_MODULES: tuple[ModuleType, ...] = (
    ai_signature_gate,
    banned_terms_scanner,
    quote_scanner,
    quote_gate_messages,
    banned_terms_gate,
)


def _renders_a_message(name: str, value: object) -> bool:
    if not callable(value):
        return False
    return (name.startswith("format_") and name.endswith("_message")) or name.endswith("_deny_message")


def _is_deny_constant(value: object) -> bool:
    return isinstance(value, str) and value.startswith("BLOCKED:")


def _deny_message_sources(module: ModuleType) -> frozenset[str]:
    """Names in *module* that render a PreToolUse deny reason.

    A message reaches the breaker as a rendered string, so the two shapes that can
    produce one are a ``format_*_message`` / ``*_deny_message`` callable and a
    module-level ``BLOCKED:`` constant.
    """
    return frozenset(
        name for name, value in vars(module).items() if _is_deny_constant(value) or _renders_a_message(name, value)
    )


def _pretooluse_context(monkeypatch: pytest.MonkeyPatch, state_dir, command: str, session: str = "leak") -> dict:
    data = {"session_id": session, "tool_name": "Bash", "tool_input": {"command": command}}
    monkeypatch.setattr(router, "STATE_DIR", state_dir)  # module constant, resolved at import
    monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")
    monkeypatch.setattr(router, "_CURRENT_DATA", data)
    return data


class TestEveryLeakMessageIsNonGrantable:
    @pytest.mark.parametrize("source", sorted(_LEAK_DENY_MESSAGES))
    def test_leak_message_is_classified_and_never_granted(
        self, source: str, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        message, gate_id = _LEAK_DENY_MESSAGES[source]
        _pretooluse_context(monkeypatch, tmp_path, f"gh issue comment 1 --body body {_FP_TOKEN}")
        assert dcb._deny_is_leak_gate(message, gate_id) is True, f"{source} fell out of the leak family"
        assert dcb.apply_deny_circuit_breaker(message, gate_id=gate_id).allow is False, (
            f"{source} was suppressed by the agent's own [fp-confirmed:] token"
        )

    def test_a_non_leak_deny_with_the_same_token_is_still_granted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The false-positive escape keeps working where it is meant to."""
        _pretooluse_context(monkeypatch, tmp_path, f"uv run pytest --no-cov -q {_FP_TOKEN}")
        assert dcb.apply_deny_circuit_breaker("BLOCKED: the orchestrator ran a heavy command.").allow is True


class TestCredentialDenySurvivesSelfConfirmation:
    """The #4218 exploit, end to end through the gate that emits the credential deny."""

    def _publish_command(self) -> str:
        return f'gh issue comment 42 --body "repro: export AWS_ACCESS_KEY_ID={_SYNTHETIC_SECRET}" {_FP_TOKEN}'

    def test_secret_publish_carrying_its_own_fp_token_still_denies(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        data = _pretooluse_context(monkeypatch, tmp_path, self._publish_command(), session="exploit")
        assert banned_terms_gate._run_banned_terms_pretool(data) is True

    def test_no_session_grant_is_recorded_for_a_secret_deny(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """A recorded grant would silently suppress every later identical publish."""
        data = _pretooluse_context(monkeypatch, tmp_path, self._publish_command(), session="exploit")
        banned_terms_gate._run_banned_terms_pretool(data)
        assert not (tmp_path / "exploit.fp-grants").exists()
        assert banned_terms_gate._run_banned_terms_pretool(data) is True


class TestLeakMessageSourceCoverage:
    """A new leak-family message cannot silently join the modules unclassified."""

    def test_every_discovered_source_is_registered(self) -> None:
        discovered = {
            f"{module.__name__.rsplit('.', 1)[-1]}.{name}"
            for module in _LEAK_MODULES
            for name in _deny_message_sources(module)
        }
        unregistered = discovered - set(_LEAK_DENY_MESSAGES) - _WARN_ONLY_SOURCES
        assert not unregistered, (
            f"unclassified leak-family deny messages: {sorted(unregistered)} — register each in "
            "_LEAK_DENY_MESSAGES (or _WARN_ONLY_SOURCES when it is a warning) so its grantability is pinned"
        )

    def test_the_discovery_finds_a_newly_added_source(self) -> None:
        """The control: without this, the coverage assertion above could pass vacuously."""
        stub = types.ModuleType("stub_leak_module")
        stub.format_new_leak_message = lambda: "BLOCKED: a newly added leak deny."
        stub._NEW_FAMILY_DENY = "BLOCKED: a newly added constant deny."
        stub.unrelated_helper = lambda: "not a message"
        assert _deny_message_sources(stub) == {"format_new_leak_message", "_NEW_FAMILY_DENY"}


class TestGateIdentityIsAuthoritative:
    """Classification keys on the gate's IDENTITY, so rewording cannot regrant a leak deny.

    The substring matcher classified only what the wording happened to say, and the
    two AI-signature reasons say none of it — so both were grantable against the
    fail-CLOSED contract in ``hooks/CLAUDE.md``. A reworded quote-scanner or
    verbatim-paste message would have fallen out the same way.
    """

    _REWORDED = "BLOCKED: a message carrying none of the marker vocabulary."

    @pytest.mark.parametrize("gate_id", sorted(dcb.LEAK_GATE_IDS))
    def test_a_leak_gate_id_is_never_granted_whatever_the_wording(
        self, gate_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        _pretooluse_context(monkeypatch, tmp_path, f"gh pr create --body b {_FP_TOKEN}")
        assert dcb._deny_is_leak_gate(self._REWORDED) is False, "the control lost its point"
        assert dcb.apply_deny_circuit_breaker(self._REWORDED, gate_id=gate_id).allow is False

    def test_an_ordinary_gate_id_is_still_granted(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """The other direction: a blanket refusal would pass the assertion above."""
        _pretooluse_context(monkeypatch, tmp_path, f"uv run pytest -q {_FP_TOKEN}")
        assert dcb.apply_deny_circuit_breaker(self._REWORDED, gate_id="plan_gate").allow is True


class TestEveryLeakHandlerStampsALeakGateId:
    """The stamping is what makes the identity classification reach the live gates."""

    def _deny_gate_id(self, monkeypatch: pytest.MonkeyPatch, handler, data: dict) -> str | None:
        stamped: list[str | None] = []
        monkeypatch.setattr(
            router, "_write_pretooluse_deny", lambda reason, *, gate_id=None: bool(stamped.append(gate_id)) or True
        )
        assert handler(data) is True, "the handler did not deny — the probe proves nothing"
        return stamped[0]

    def test_banned_terms_credential_deny(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        data = _pretooluse_context(
            monkeypatch, tmp_path, f'gh issue comment 42 --body "export AWS_ACCESS_KEY_ID={_SYNTHETIC_SECRET}"'
        )
        assert self._deny_gate_id(monkeypatch, banned_terms_gate._run_banned_terms_pretool, data) in dcb.LEAK_GATE_IDS

    @pytest.mark.parametrize(
        ("stdout", "stderr"),
        [("AI-signature scan: 1 banned trailer(s)\n", ""), ("", "Traceback (most recent call last)")],
        ids=["finding", "scanner_error"],
    )
    def test_ai_signature(self, stdout: str, stderr: str, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        data = _pretooluse_context(monkeypatch, tmp_path, _AI_SIG_COMMIT)
        _stub_ai_sig_scanner(monkeypatch, stdout, stderr)
        assert self._deny_gate_id(monkeypatch, router._run_block_ai_signature, data) in dcb.LEAK_GATE_IDS


class TestAiSignatureDenyIsNeverGranted:
    """The reported hole, end to end through the gate that emits it."""

    def test_a_publish_carrying_its_own_fp_token_still_denies(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        data = _pretooluse_context(monkeypatch, tmp_path, f"{_AI_SIG_COMMIT} {_FP_TOKEN}", session="aisig")
        _stub_ai_sig_scanner(monkeypatch, "AI-signature scan: 1 banned trailer(s)\n")
        assert router._run_block_ai_signature(data) is True

    def test_no_session_grant_is_recorded(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """A recorded grant would silently suppress every later identical publish."""
        data = _pretooluse_context(monkeypatch, tmp_path, f"{_AI_SIG_COMMIT} {_FP_TOKEN}", session="aisig")
        _stub_ai_sig_scanner(monkeypatch, "AI-signature scan: 1 banned trailer(s)\n")
        router._run_block_ai_signature(data)
        assert not (tmp_path / "aisig.fp-grants").exists()
        assert router._run_block_ai_signature(data) is True
