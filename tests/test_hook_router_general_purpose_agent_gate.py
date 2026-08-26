# test-path: cross-cutting
# Exercises the hooks/scripts/general_purpose_agent_gate.py PreToolUse handler
# wired into hook_router.py (no src/teatree mirror), so it spans packages.
"""A blank ``general-purpose`` sub-agent must be REFUSED for managed-repo work.

The Skills-First rule was advisory: a ``PreToolUse`` hook injected context and
never denied, so it fired about a dozen times in one session and was overridden
every time. A rule that has failed again needs a gate.

The gate is deliberately narrow, so both directions are pinned. It denies a
``general-purpose`` (or subagent-type-less) ``Agent``/``Task`` dispatch whose
brief names a repo the overlay registry declares managed; it leaves a typed
sub-agent, a non-managed brief, and the ``TaskCreate``/``TaskUpdate`` todo
writes alone. The deny is asserted through the router SUBPROCESS as well as
in-process, because the harness honours a deny only as the nested
``hookSpecificOutput`` envelope PLUS exit 2 — an exit-0 deny does nothing.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import general_purpose_agent_gate as gate

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"

MANAGED_SLUG = "acme-engineering/acme-product"
MANAGED_BRIEF = "fix the loan-offer serializer in acme-product"
REGISTRY = {"t3-demo": {"workspace_repos": [MANAGED_SLUG], "frontend_repos": ["acme-client-workspace"]}}

TYPED_AGENTS = (
    "t3:coder",
    "t3:debugger",
    "t3:tester",
    "t3:e2e",
    "t3:reviewer",
    "t3:bughunter",
    "t3:planner",
    "t3:shipper",
)


def _event(tool_input: dict, *, tool_name: str = "Agent") -> dict:
    return {"session_id": "sess-blank-agent", "tool_name": tool_name, "tool_input": tool_input}


def _run(tool_input: dict, *, tool_name: str = "Agent") -> tuple[bool, dict | None]:
    buf = StringIO()
    with (
        patch.object(gate, "managed_repo_tokens", lambda: frozenset({"acme-product", "acme-engineering"})),
        patch("sys.stdout", buf),
    ):
        blocked = router.handle_block_general_purpose_agent(_event(tool_input, tool_name=tool_name))
    raw = buf.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


def _chain_denies(tool_input: dict, *, tool_name: str = "Agent") -> bool:
    """True iff ANY registered PreToolUse handler refuses this dispatch."""
    buf = StringIO()
    event = _event(tool_input, tool_name=tool_name)
    with patch.object(gate, "managed_repo_tokens", lambda: frozenset({"acme-product"})), patch("sys.stdout", buf):
        return any(handler(event) for handler in router._HANDLERS["PreToolUse"])


class TestRegisteredChainRefusesTheBlankDispatch:
    """The anti-vacuous proof: BEFORE this gate no registered handler said no."""

    def test_some_registered_handler_denies(self) -> None:
        assert _chain_denies({"subagent_type": "general-purpose", "prompt": MANAGED_BRIEF}) is True

    def test_typed_dispatch_still_passes_the_whole_chain(self) -> None:
        assert _chain_denies({"subagent_type": "t3:coder", "prompt": MANAGED_BRIEF}) is False


class TestBlankDispatchIsDenied:
    @pytest.mark.parametrize(
        ("tool_input", "tool_name"),
        [
            ({"subagent_type": "general-purpose", "prompt": MANAGED_BRIEF}, "Agent"),
            ({"subagent_type": "general-purpose", "description": "acme-product sync"}, "Agent"),
            ({"prompt": MANAGED_BRIEF}, "Agent"),
            ({"subagent_type": "", "prompt": MANAGED_BRIEF}, "Agent"),
            ({"subagent_type": "general-purpose", "prompt": MANAGED_BRIEF}, "Task"),
            ({"subagent_type": "general-purpose", "prompt": "cd ~/wt-acme-product-42 && run it"}, "Agent"),
        ],
    )
    def test_denied(self, tool_input: dict, tool_name: str) -> None:
        blocked, payload = _run(tool_input, tool_name=tool_name)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_refusal_names_every_typed_agent_and_the_escape(self) -> None:
        reason = gate.refusal("acme-product")
        for agent in (*TYPED_AGENTS, "Explore"):
            assert agent in reason, f"the refusal must name {agent}: {reason!r}"
        assert "[general-purpose-ok:" in reason
        assert "acme-product" in reason


class TestNarrowlyScoped:
    @pytest.mark.parametrize("subagent_type", [*TYPED_AGENTS, "Explore", "teatree-mcp-worker"])
    def test_typed_dispatch_passes_untouched(self, subagent_type: str) -> None:
        blocked, payload = _run({"subagent_type": subagent_type, "prompt": MANAGED_BRIEF})
        assert blocked is False
        assert payload is None

    @pytest.mark.parametrize(
        "tool_input",
        [
            {"subagent_type": "general-purpose", "prompt": "research the best CI cache strategy in general"},
            # A managed token must not match inside a longer WORD: `aimed` is prose.
            {"subagent_type": "general-purpose", "prompt": "the change acme-producer aimed at"},
        ],
    )
    def test_unmanaged_brief_passes(self, tool_input: dict) -> None:
        blocked, payload = _run(tool_input)
        assert blocked is False
        assert payload is None

    @pytest.mark.parametrize("tool_name", ["TaskCreate", "TaskUpdate", "Bash"])
    def test_non_dispatch_tool_is_ignored(self, tool_name: str) -> None:
        blocked, payload = _run({"description": MANAGED_BRIEF, "command": "ls"}, tool_name=tool_name)
        assert blocked is False
        assert payload is None

    def test_empty_registry_allows(self) -> None:
        buf = StringIO()
        with patch.object(gate, "overlays_registry", dict), patch("sys.stdout", buf):
            blocked = router.handle_block_general_purpose_agent(
                _event({"subagent_type": "general-purpose", "prompt": MANAGED_BRIEF})
            )
        assert blocked is False
        assert buf.getvalue().strip() == ""


class TestNeverLockout:
    def test_per_call_token_allows(self) -> None:
        prompt = f"{MANAGED_BRIEF} [general-purpose-ok: vendoring an unrelated OSS diff]"
        blocked, payload = _run({"subagent_type": "general-purpose", "prompt": prompt})
        assert blocked is False
        assert payload is None

    @pytest.mark.parametrize("marker", ["[general-purpose-ok: ]", "[general-purpose-ok:]", "[general-purpose-ok]"])
    def test_empty_reason_does_not_allow(self, marker: str) -> None:
        blocked, _ = _run({"subagent_type": "general-purpose", "prompt": f"{MANAGED_BRIEF} {marker}"})
        assert blocked is True

    def test_token_buried_past_the_scanned_prefix_does_not_allow(self) -> None:
        buried = f"{'x' * 600} [general-purpose-ok: too late] {MANAGED_BRIEF}"
        blocked, _ = _run({"subagent_type": "general-purpose", "prompt": buried})
        assert blocked is True

    def test_kill_switch_disables_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_teatree_bool_setting",
            lambda key, default=True: False if key == "general_purpose_agent_gate_enabled" else default,
        )
        blocked, payload = _run({"subagent_type": "general-purpose", "prompt": MANAGED_BRIEF})
        assert blocked is False
        assert payload is None


class TestManagedRepoTokens:
    def test_tokens_are_the_registry_slugs_and_their_segments(self) -> None:
        with patch.object(gate, "overlays_registry", lambda: REGISTRY):
            tokens = gate.managed_repo_tokens()
        assert {MANAGED_SLUG, "acme-engineering", "acme-product", "acme-client-workspace"} <= tokens

    def test_a_malformed_registry_entry_is_skipped(self) -> None:
        with patch.object(gate, "overlays_registry", lambda: {"a": "not-a-dict", "b": {"workspace_repos": "nope"}}):
            assert gate.managed_repo_tokens() == frozenset()


def _seed_config_db(path: Path, rows: dict[str, object]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, json.dumps(value))
            )
        conn.commit()
    finally:
        conn.close()


def _run_router(payload: dict, *, settings: dict[str, object]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home, "USERPROFILE": home}
        env.pop("XDG_DATA_HOME", None)
        db = Path(home) / "db.sqlite3"
        _seed_config_db(db, settings)
        env["T3_CONFIG_DB"] = str(db)
        return subprocess.run(
            [sys.executable, str(HOOK_ROUTER), "--event", "PreToolUse"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=env,
        )


class TestHarnessHonouredDenyEnvelope:
    """A deny is honoured ONLY as the nested envelope PLUS exit 2 — assert both."""

    def test_blank_dispatch_denies_with_exit_2_and_the_nested_envelope(self) -> None:
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose", "prompt": MANAGED_BRIEF}}
        result = _run_router(payload, settings={"overlays": REGISTRY})

        assert result.returncode == 2, f"a deny must exit 2 (got {result.returncode}); stdout={result.stdout!r}"
        out = json.loads(result.stdout)
        nested = out["hookSpecificOutput"]
        assert nested["hookEventName"] == "PreToolUse"
        assert nested["permissionDecision"] == "deny"
        assert "t3:coder" in nested["permissionDecisionReason"]
        assert out["permissionDecision"] == "deny"

    def test_typed_dispatch_exits_0_with_no_decision(self) -> None:
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "t3:coder", "prompt": MANAGED_BRIEF}}
        result = _run_router(payload, settings={"overlays": REGISTRY})
        assert result.returncode == 0, f"a typed dispatch must not be denied; stdout={result.stdout!r}"

    def test_kill_switch_exits_0(self) -> None:
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose", "prompt": MANAGED_BRIEF}}
        settings = {"overlays": REGISTRY, "general_purpose_agent_gate_enabled": False}
        result = _run_router(payload, settings=settings)
        assert result.returncode == 0, f"the kill-switch must allow; stdout={result.stdout!r}"


class TestInternalErrorFailsOpenLoudly:
    """A gate bug must never wedge a dispatch, and must never do so silently."""

    def test_raising_gate_allows_and_names_the_fault_on_stderr(self) -> None:
        def boom(_data: dict) -> bool:
            msg = "injected fault"
            raise RuntimeError(msg)

        buf = StringIO()
        err = StringIO()
        with (
            patch.dict(router._HANDLERS, {"PreToolUse": [boom]}),
            patch("sys.stdout", buf),
            patch("sys.stderr", err),
            patch.object(sys, "argv", ["hook_router.py", "--event", "PreToolUse"]),
            patch.object(router, "_read_input", lambda: {"tool_name": "Agent", "tool_input": {}}),
        ):
            router.main()

        assert buf.getvalue().strip() == "", "a crashed gate must not decide"
        assert "injected fault" in err.getvalue(), f"the cause must be visible: {err.getvalue()!r}"
