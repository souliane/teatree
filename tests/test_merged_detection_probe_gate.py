# test-path: cross-cutting
# Exercises the hooks/scripts/merged_detection_probe_gate.py PreToolUse handler wired into
# hook_router.py against the teatree.hooks.merged_detection_probe detection leaf, so it
# spans packages.
"""A hand-rolled "is this branch landed?" probe earns one advisory, never a deny (#4070).

An orchestrator ran ``git cherry origin/main HEAD`` across ~18 worktrees, read four
branches as unmerged, escalated three to the owner as false completions and dispatched a
shipper to push them. Three were already on main via squash-merge — which rewrites SHAs,
so a per-commit / ancestor test misreads it. The canonical three-layer classifier existed
and was not reached for.

The gate is WARN-only by design: a coder mid-rebase running ``git cherry`` is
indistinguishable from an agent deciding landed-ness, and that ambiguity is exactly why it
must not deny. So both directions are pinned here — the incident shape warns, and the
legitimate two-SHA ``merge-base --is-ancestor`` provenance proof
(``core/management/commands/repro.py``) does not.
"""

import ast
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.merged_detection_probe_gate as gate
from hooks.scripts.merged_detection_probe_gate import handle_warn_merged_detection_probe


def _bash_event(command: str) -> dict:
    return {"session_id": "sess-merge-detect", "tool_name": "Bash", "tool_input": {"command": command}}


def _advisory(command: str) -> str:
    buf = StringIO()
    with patch("sys.stderr", buf):
        blocked = handle_warn_merged_detection_probe(_bash_event(command))
    assert blocked is False, "the merged-detection gate must never deny"
    return buf.getvalue()


class TestTheIncidentShapeWarns:
    @pytest.mark.parametrize(
        "command",
        [
            "git cherry origin/main HEAD",
            "git cherry main HEAD",
            "git cherry origin/master HEAD",
            "git branch --merged origin/main",
            "git branch --merged",
            "git merge-base --is-ancestor HEAD origin/main",
            "git log HEAD --not origin/main --oneline",
            "cd /some/worktree && git cherry origin/main HEAD",
            "git -C /some/worktree cherry origin/main HEAD",
        ],
    )
    def test_advisory_names_the_sanctioned_command(self, command: str) -> None:
        advisory = _advisory(command)

        assert "workspace branch-verdict" in advisory
        assert "squash-merge" in advisory


class TestLegitimateShapesAreSilent:
    def test_repro_two_sha_ancestor_proof_does_not_warn(self) -> None:
        # ``core/management/commands/repro.py`` proves the RED tree is an ancestor of the
        # GREEN tree. Neither operand is default-branch-shaped — that shape is the whole
        # thing separating it from a landed-ness question.
        red, green = "a" * 40, "b" * 40

        assert _advisory(f"git merge-base --is-ancestor {red} {green}") == ""

    @pytest.mark.parametrize(
        "command",
        [
            "git cherry upstream/topic HEAD",
            "git branch --merged release/2024-06",
            "git log origin/main..HEAD --oneline",
            "git diff origin/main...HEAD",
            "git log HEAD --not --remotes --oneline",
            "echo 'run git cherry origin/main HEAD to check'",
            "grep -rn 'git cherry origin/main' docs/",
        ],
    )
    def test_no_advisory(self, command: str) -> None:
        assert _advisory(command) == ""

    def test_non_bash_tool_is_ignored(self) -> None:
        event = {"session_id": "s", "tool_name": "Edit", "tool_input": {"new_string": "git cherry origin/main HEAD"}}

        assert handle_warn_merged_detection_probe(event) is False


class TestNeverLockoutTrio:
    def test_per_call_token_suppresses_the_advisory(self) -> None:
        assert _advisory("git cherry origin/main HEAD  # [merge-detect-ok: comparing two forks]") == ""

    def test_an_empty_token_reason_does_not_suppress(self) -> None:
        assert _advisory("git cherry origin/main HEAD  # [merge-detect-ok: ]") != ""

    def test_kill_switch_silences_the_gate(self) -> None:
        with patch.object(gate, "teatree_bool_setting", return_value=False):
            assert _advisory("git cherry origin/main HEAD") == ""

    def test_a_raising_resolver_fails_open_and_silent(self) -> None:
        with patch.object(gate, "teatree_bool_setting", side_effect=RuntimeError("db is wedged")):
            assert _advisory("git cherry origin/main HEAD") == ""


class TestRegisteredAsWarnOnly:
    def test_handler_is_in_the_pretooluse_chain(self) -> None:
        assert handle_warn_merged_detection_probe in router._HANDLERS["PreToolUse"]

    def test_the_whole_chain_allows_the_incident_shape(self) -> None:
        # Anti-vacuity in the deny direction: no registered handler may refuse the probe —
        # the advisory is the entire enforcement.
        buf = StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", StringIO()):
            denied = any(
                handler(_bash_event("git cherry origin/main HEAD")) for handler in router._HANDLERS["PreToolUse"]
            )

        assert denied is False

    def test_the_module_never_reaches_a_deny_emitter(self) -> None:
        # Structural pin, copied from the #1442 investigation gate: neither the handler nor
        # any future helper in this module may call a deny emitter.
        tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
        deny_emitters = {"emit_pretooluse_deny", "_fail_open_or_deny"}
        module_calls = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert deny_emitters.isdisjoint(module_calls)

    def test_every_handler_return_is_false(self) -> None:
        tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "handle_warn_merged_detection_probe"
        )
        returns = [node.value for node in ast.walk(handler) if isinstance(node, ast.Return)]

        assert returns, "the handler must return explicitly"
        assert all(isinstance(value, ast.Constant) and value.value is False for value in returns)
