# test-path: cross-cutting
# Exercises the hooks/scripts/orchestrator_delegation_gate.py PreToolUse handler
# wired into hook_router.py (no src/teatree mirror), so it spans packages.
"""An UNBOUNDED orchestrator read must be REFUSED; a bounded routing read must not.

The orchestrate-only boundary had two enforcement points and neither covered the
measured failure: the heavy-Bash gate keys on DURATION (and a sweep is fast), and
the investigation nudge is structurally forbidden from denying. So an attended
session ran dozens of inline ``rg`` sweeps and ``… | python3 -c`` reductions with
nothing refusing.

Both directions are pinned, because a gate this broad is worthless in either
failure mode. The MUST-REFUSE corpus is the anti-vacuity proof — every row is a
shape observed in the session that motivated the gate. The MUST-ALLOW corpus is
the lockout proof: it is the orchestrator's own routing vocabulary, and a
regression there wedges the operator.
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

import hooks.scripts.hook_router as router
from hooks.scripts import orchestrator_delegation_gate as gate
from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI, LANE_SDK, LANE_UNKNOWN
from teatree.cli.teatree_gate import ORCHESTRATOR_DELEGATION_GATE_KEY, register_gate_commands

SESSION = "sess-delegation"

# Shapes measured in the session that motivated the gate: a sweep with no ceiling,
# and an output reduced by a program because it was too large to read.
MUST_REFUSE = (
    pytest.param("rg 'loop_lease' src/", id="rg_tree_sweep"),
    pytest.param("rg -n TODO", id="rg_bare_cwd_sweep"),
    pytest.param("grep -rn 'ownership_status' src/teatree", id="grep_recursive"),
    pytest.param("grep -n -R pattern .", id="grep_recursive_second_flag"),
    pytest.param("git grep -n 'def handle_'", id="git_grep"),
    pytest.param("find . -name '*.py'", id="find_tree_walk"),
    # A glob is how a tree walk is SPELLED, so it must not read as a named file.
    pytest.param("rg pattern 'src/**/*.py'", id="glob_operand_is_not_a_named_file"),
    pytest.param("cd /repo && rg 'gate_enabled'", id="sweep_after_and"),
    pytest.param(
        "t3 teatree tasks list --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'",
        id="json_reduced_by_python",
    ),
    pytest.param(
        "docker compose logs | python3 -c 'import sys; print(sys.stdin.read()[-4000:])'",
        id="log_trawl_parse",
    ),
    pytest.param('cat build.log | node -e "process.stdin.on(0, d => console.log(d.length))"', id="node_parse"),
)

# The orchestrator's own routing vocabulary. Every row answers ONE question with a
# bounded answer; refusing any of them makes routing impossible.
MUST_ALLOW = (
    pytest.param("git status --short", id="git_status"),
    pytest.param("docker ps --format '{{.Names}}'", id="docker_ps_liveness"),
    pytest.param("gh pr view 4001 --json state,mergeable", id="pr_state"),
    pytest.param("t3 teatree tasks list --json", id="queue_depth"),
    pytest.param("t3 teatree loop stats --json | jq '.queued'", id="queue_depth_field_extract"),
    pytest.param("docker ps | grep teatree-worker", id="grep_as_downstream_filter"),
    pytest.param("cat .t3-env.cache", id="point_read_of_a_config"),
    pytest.param("head -50 src/teatree/core/models.py", id="bounded_file_read"),
    pytest.param("sed -n '1,80p' AGENTS.md", id="bounded_range_read"),
    pytest.param("rg -m 5 'def handle_' hooks/scripts", id="sweep_with_count_bound"),
    pytest.param("rg 'def handle_' hooks/scripts | head -20", id="sweep_piped_into_head"),
    pytest.param("grep -n pattern one_file.py", id="non_recursive_grep"),
    pytest.param("rg -n pattern one_file.py", id="rg_over_one_named_file_is_bounded"),
    pytest.param("git grep -n pattern -- src/teatree/core/models.py", id="git_grep_over_one_named_file"),
    pytest.param("t3 teatree tasks list --json | jq '. | length'", id="queue_depth_reduced_by_jq"),
    pytest.param("find --help", id="find_help_is_not_a_walk"),
    pytest.param("python3 -c 'print(1 + 1)'", id="bare_interpreter_is_not_a_parse"),
    pytest.param("git log --oneline -5 | rg fix", id="rg_downstream_of_a_pipe_is_a_filter"),
    pytest.param("t3 teatree gate delegation disable", id="the_self_rescue_verb"),
    pytest.param("git commit -m '8123 fix the sweep'", id="commit_message_digits_do_not_bound_anything"),
)

_UNGATED_TOOLS = (
    pytest.param("Agent"),
    pytest.param("Task"),
    pytest.param("TaskCreate"),
    pytest.param("TaskUpdate"),
    pytest.param("SendMessage"),
    pytest.param("AskUserQuestion"),
    pytest.param("Read"),
    pytest.param("mcp__slack__send_message"),
    pytest.param("mcp__notion__fetch"),
)

# The native spelling of the SAME sweep. Before these arms an orchestrator refused
# `Bash(rg ...)` switched to `Grep(...)` and never met the gate again.
NATIVE_MUST_REFUSE = (
    pytest.param("Grep", {"pattern": "loop_lease", "path": "src/"}, id="grep_tree_sweep"),
    pytest.param("Grep", {"pattern": "TODO"}, id="grep_bare_cwd_sweep"),
    pytest.param("Grep", {"pattern": "def handle_", "path": "hooks/scripts"}, id="grep_over_a_directory"),
    pytest.param(
        "Grep",
        {"pattern": "x", "path": "src/", "output_mode": "files_with_matches"},
        id="grep_file_list_is_still_unbounded",
    ),
    pytest.param("Grep", {"pattern": "x", "path": "src/", "head_limit": 0}, id="grep_zero_head_limit_bounds_nothing"),
    pytest.param("Glob", {"pattern": "**/*.py"}, id="glob_every_file_of_a_kind"),
    pytest.param("Glob", {"pattern": "src/teatree/config/*.py"}, id="glob_extension_only_under_a_dir"),
    pytest.param("Glob", {"pattern": "**/*"}, id="glob_everything"),
)

# The orchestrator's own native routing vocabulary — refusing any of these makes
# locating one file impossible.
NATIVE_MUST_ALLOW = (
    pytest.param("Grep", {"pattern": "x", "path": "src/", "head_limit": 20}, id="grep_with_a_count_bound"),
    pytest.param("Grep", {"pattern": "x", "path": "src/teatree/core/models.py"}, id="grep_over_one_named_file"),
    pytest.param("Grep", {"pattern": "x", "path": "hooks/scripts/hook_router.py"}, id="grep_over_one_named_hook"),
    pytest.param("Glob", {"pattern": "**/settings_editor.py"}, id="glob_names_one_file"),
    pytest.param("Glob", {"pattern": "**/*_scanner.py"}, id="glob_names_a_file_family"),
    pytest.param("Glob", {"pattern": "docs/**/README.md"}, id="glob_names_a_file_under_a_tree"),
    pytest.param("Grep", {}, id="a_malformed_grep_is_not_evidence_of_investigation"),
    pytest.param("Glob", {}, id="a_malformed_glob_is_not_evidence_of_investigation"),
)


def _event(command: str) -> dict:
    return {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": command}}


def _native_event(tool_name: str, tool_input: dict) -> dict:
    return {"session_id": SESSION, "tool_name": tool_name, "tool_input": tool_input}


def _verdict(
    event: dict,
    *,
    lane: str = LANE_INTERACTIVE_CLI,
    engaged: bool = True,
    enabled: bool = True,
    fail_open: bool = False,
) -> tuple[bool, dict | None]:
    """``(denied, deny payload)`` for one event, with every ambient signal pinned."""
    buf = StringIO()
    with (
        patch.object(gate, "session_lane", lambda: lane),
        patch.object(gate, "_engaged", lambda _sid: engaged),
        patch.object(gate, "_gate_enabled", lambda: enabled),
        patch.object(router, "_is_self_rescue", lambda _cmd: False),
        patch.object(router, "_danger_gate_fail_open_enabled", lambda: fail_open),
        patch("sys.stdout", buf),
    ):
        denied = router.handle_block_undelegated_investigation(event)
    raw = buf.getvalue().strip()
    return denied, (json.loads(raw) if raw else None)


def _chain_denies(event: dict) -> bool:
    """True iff ANY registered PreToolUse handler refuses this call."""
    buf = StringIO()
    with (
        patch.object(gate, "session_lane", lambda: LANE_INTERACTIVE_CLI),
        patch.object(gate, "_engaged", lambda _sid: True),
        patch.object(gate, "_gate_enabled", lambda: True),
        patch.object(router, "_is_self_rescue", lambda _cmd: False),
        patch.object(router, "_danger_gate_fail_open_enabled", lambda: False),
        patch("sys.stdout", buf),
    ):
        return any(handler(event) for handler in router._HANDLERS["PreToolUse"])


class TestTheGateFires:
    """MUST-REFUSE — the anti-vacuity corpus."""

    @pytest.mark.parametrize("command", MUST_REFUSE)
    def test_unbounded_read_is_refused(self, command: str) -> None:
        denied, payload = _verdict(_event(command))
        assert denied is True, f"the gate did not fire on {command!r}"
        assert payload is not None, "a deny returned no payload"
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert payload["hookSpecificOutput"]["gate_id"] == "orchestrator_delegation_gate"

    def test_the_registered_chain_refuses_a_sweep(self) -> None:
        """Before this gate, NO registered PreToolUse handler said no to an inline ``rg``."""
        assert _chain_denies(_event("rg 'loop_lease' src/")) is True

    def test_the_refusal_teaches_both_levers(self) -> None:
        _denied, payload = _verdict(_event("rg pattern src/"))
        assert payload is not None, "a deny returned no payload"
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Explore" in reason
        assert "t3:debugger" in reason
        assert "--max-count" in reason
        assert "t3 <overlay> gate delegation disable" in reason


class TestTheGateStaysOutOfTheWay:
    """MUST-ALLOW — the lockout corpus: the orchestrator's routing vocabulary."""

    @pytest.mark.parametrize("command", MUST_ALLOW)
    def test_routing_read_is_allowed(self, command: str) -> None:
        denied, payload = _verdict(_event(command))
        assert denied is False, f"the gate wrongly refused the routing read {command!r}"
        assert payload is None

    @pytest.mark.parametrize("tool_name", _UNGATED_TOOLS)
    def test_a_tool_that_is_not_a_read_is_allowed_by_construction(self, tool_name: str) -> None:
        """Not by an allowlist that goes stale — a connector added tomorrow passes too."""
        event = {"session_id": SESSION, "tool_name": tool_name, "tool_input": {"prompt": "rg -r everything"}}
        assert _verdict(event)[0] is False

    @pytest.mark.parametrize(("tool_name", "tool_input"), NATIVE_MUST_ALLOW)
    def test_a_bounded_native_read_is_allowed(self, tool_name: str, tool_input: dict) -> None:
        denied, payload = _verdict(_native_event(tool_name, tool_input))
        assert denied is False, f"the gate wrongly refused the bounded {tool_name}({tool_input})"
        assert payload is None


class TestTheNativeSpellingIsGatedToo:
    """The route-around #4585 named: `Bash(rg ...)` refused, `Grep(...)` allowed."""

    @pytest.mark.parametrize(("tool_name", "tool_input"), NATIVE_MUST_REFUSE)
    def test_an_unbounded_native_read_is_refused(self, tool_name: str, tool_input: dict) -> None:
        denied, payload = _verdict(_native_event(tool_name, tool_input))
        assert denied is True, f"the gate did not fire on {tool_name}({tool_input})"
        assert payload is not None, "a deny returned no payload"
        assert payload["hookSpecificOutput"]["gate_id"] == "orchestrator_delegation_gate"

    def test_the_grep_refusal_teaches_greps_own_bounds(self) -> None:
        _denied, payload = _verdict(_native_event("Grep", {"pattern": "x", "path": "src/"}))
        assert payload is not None, "a deny returned no payload"
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        assert "head_limit" in reason
        assert "--max-count" not in reason, "a Grep refusal must not teach a Bash flag"

    def test_the_glob_refusal_teaches_globs_own_bound(self) -> None:
        _denied, payload = _verdict(_native_event("Glob", {"pattern": "**/*.py"}))
        assert payload is not None, "a deny returned no payload"
        assert "**/*_scanner.py" in payload["hookSpecificOutput"]["permissionDecisionReason"]

    def test_the_kill_switch_allows_a_native_read(self) -> None:
        assert _verdict(_native_event("Grep", {"pattern": "x", "path": "src/"}), enabled=False)[0] is False

    def test_a_subagent_sweeping_natively_is_doing_its_job(self) -> None:
        event = _native_event("Grep", {"pattern": "x", "path": "src/"}) | {"agent_id": "agent-7"}
        assert _verdict(event)[0] is False

    def test_only_the_delegation_gate_refuses_a_native_read(self) -> None:
        """The matcher now routes Grep/Glob to EVERY PreToolUse handler, not just this one."""
        assert _chain_denies(_native_event("Grep", {"pattern": "x", "path": "src/", "head_limit": 5})) is False
        assert _chain_denies(_native_event("Glob", {"pattern": "**/settings_editor.py"})) is False

    def test_the_registered_chain_refuses_the_native_sweep(self) -> None:
        assert _chain_denies(_native_event("Grep", {"pattern": "loop_lease", "path": "src/"})) is True


class TestScopeIsTheOrchestrator:
    def test_a_subagent_sweeping_is_doing_its_job(self) -> None:
        event = _event("rg pattern src/") | {"agent_id": "agent-7"}
        assert _verdict(event)[0] is False

    def test_the_sdk_lane_is_the_factorys_own_worker(self) -> None:
        assert _verdict(_event("rg pattern src/"), lane=LANE_SDK)[0] is False

    def test_an_unreadable_lane_allows(self) -> None:
        assert _verdict(_event("rg pattern src/"), lane=LANE_UNKNOWN)[0] is False

    def test_an_unengaged_session_is_not_governed(self) -> None:
        assert _verdict(_event("rg pattern src/"), engaged=False)[0] is False


class TestNeverLockout:
    def test_the_per_call_token_allows(self) -> None:
        assert _verdict(_event("rg pattern src/  # [delegate-ok: sizing the blast radius]"))[0] is False

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("rg pattern src/ # [delegate-ok:]", id="no_reason"),
            pytest.param("rg pattern src/ # [delegate-ok: ]", id="blank_reason"),
        ],
    )
    def test_an_empty_reason_does_not_unblock(self, command: str) -> None:
        assert _verdict(_event(command))[0] is True

    def test_the_kill_switch_allows(self) -> None:
        assert _verdict(_event("rg pattern src/"), enabled=False)[0] is False

    def test_the_master_fail_open_switch_allows(self) -> None:
        assert _verdict(_event("rg pattern src/"), fail_open=True)[0] is False

    def test_a_self_rescue_command_is_never_refused(self) -> None:
        """Belt-and-braces on top of the rescue verbs not matching either arm."""
        buf = StringIO()
        with (
            patch.object(gate, "session_lane", lambda: LANE_INTERACTIVE_CLI),
            patch.object(gate, "_engaged", lambda _sid: True),
            patch.object(gate, "_gate_enabled", lambda: True),
            patch.object(router, "_is_self_rescue", lambda _cmd: True),
            patch("sys.stdout", buf),
        ):
            assert router.handle_block_undelegated_investigation(_event("rg pattern src/")) is False

    def test_a_broken_probe_fails_open(self) -> None:
        def boom() -> str:
            msg = "lane resolver exploded"
            raise RuntimeError(msg)

        with patch.object(gate, "session_lane", boom):
            assert router.handle_block_undelegated_investigation(_event("rg pattern src/")) is False


class TestTheKillSwitchIsReachableFromTheCLI:
    def test_the_gate_key_is_registered(self) -> None:
        assert ORCHESTRATOR_DELEGATION_GATE_KEY == "orchestrator_delegation_gate_enabled"

    def test_the_disable_verb_exists(self) -> None:
        app = typer.Typer()
        register_gate_commands(app)
        result = CliRunner().invoke(app, ["gate", "delegation", "--help"])
        assert result.exit_code == 0
        assert "disable" in result.stdout


class TestTheMatcherRoutesTheNativeTools:
    """A gate only fires for a tool wired into its event's matcher."""

    def test_hooks_json_routes_grep_and_glob_to_pretooluse(self) -> None:
        config = json.loads((Path(router.__file__).resolve().parents[2] / "hooks" / "hooks.json").read_text())
        covered = {tool for entry in config["hooks"]["PreToolUse"] for tool in entry.get("matcher", "").split("|")}
        assert {"Grep", "Glob"} <= covered, f"Grep/Glob are not routed to PreToolUse: {sorted(covered)}"
