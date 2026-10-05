# test-path: cross-cutting -- the shipped PreToolUse router enforces an explicit
# user plan-first contract before any action/question/dispatch tool can execute.
"""An explicit multi-ticket plan-first request is a runtime contract, not prose.

The gate is deliberately narrow: it activates only when the owner's own latest
prompt orders a change, binds a plan before it, and names at least two ticket
IDs.  Once active, each named target needs its own prospective implementation
and verification sequence in visible assistant text before the first governed
tool call; reads stay open, while a task must itself record the plan. This pins both
anti-vacuity (the two measured bypasses are denied) and never-lockout
(unrelated and unreadable contexts pass).
"""

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import deny_circuit_breaker
from hooks.scripts import visible_plan_gate as gate
from teatree.config import COLD_HOOK_SETTINGS
from teatree.eval.loader import load_eval_yaml
from teatree.hooks.self_rescue import is_self_rescue

_USER = (
    "Handle PROJ-4521 and PROJ-4242. Binding rule: present a per-ticket "
    "plan in your response before any edit, commit, or push."
)
_GOOD_PLAN = """Per-ticket plan before any action:

PROJ-4521: inspect forms.py, implement the literal-gate removal, run focused tests, then verify before shipping.
PROJ-4242: inspect views.py, implement the visibility filter, run the two-user tests, then verify before shipping.
"""


def _write_transcript(tmp_path: Path, *, user: str = _USER, assistant_text: str = "") -> Path:
    entries = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": user}]}},
    ]
    if assistant_text:
        entries.append(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
            }
        )
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
    return path


def _event(path: Path, tool_name: str, *, agent_id: str = "") -> dict:
    event = {
        "session_id": "sess-plan-first",
        "transcript_path": str(path),
        "tool_name": tool_name,
        "tool_input": {"command": 'echo "dispatch placeholder -- see Task calls"'},
    }
    if agent_id:
        event["agent_id"] = agent_id
    return event


def _verdict(event: dict) -> tuple[bool, dict | None]:
    stdout = StringIO()
    with (
        patch.object(router, "_is_self_rescue", lambda _cmd: False),
        patch.object(router, "_danger_gate_fail_open_enabled", lambda: False),
        patch("sys.stdout", stdout),
    ):
        denied = gate.handle_enforce_visible_plan_before_tools(event)
    raw = stdout.getvalue().strip()
    return denied, json.loads(raw) if raw else None


@pytest.mark.parametrize("tool_name", ["Bash", "AskUserQuestion", "Edit", "TaskCreate", "TaskUpdate"])
def test_first_placeholder_or_question_is_denied_before_execution(tmp_path: Path, tool_name: str) -> None:
    denied, payload = _verdict(_event(_write_transcript(tmp_path), tool_name))

    assert denied is True
    assert payload is not None
    decision = payload["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["gate_id"] == "visible_plan_gate"
    assert "PROJ-4521" in decision["permissionDecisionReason"]
    assert "PROJ-4242" in decision["permissionDecisionReason"]


@pytest.mark.parametrize("tool_name", ["Bash", "AskUserQuestion", "Edit", "Write", "TaskCreate", "TaskUpdate", "Agent"])
def test_structured_visible_plan_allows_governed_tools(tmp_path: Path, tool_name: str) -> None:
    denied, payload = _verdict(_event(_write_transcript(tmp_path, assistant_text=_GOOD_PLAN), tool_name))

    assert denied is False
    assert payload is None


@pytest.mark.parametrize(
    "text",
    [
        "PROJ-4521 and PROJ-4242 are both in scope. No edits yet.",
        "PROJ-4521: inspect forms.py, implement the fix, run tests, verify. PROJ-4242 is also in scope.",
        "PROJ-4521 and PROJ-4242: inspect files, implement changes, run tests, and verify.",
    ],
    ids=["names_only", "second_target_has_no_actions", "shared_actions_are_not_per_target"],
)
def test_names_or_one_target_actions_do_not_unlock_the_gate(tmp_path: Path, text: str) -> None:
    assert _verdict(_event(_write_transcript(tmp_path, assistant_text=text), "Bash"))[0] is True


def test_tool_result_entries_do_not_hide_the_binding_user_request(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path, assistant_text=_GOOD_PLAN)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n"
            + json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}],
                    },
                }
            )
        )

    assert _verdict(_event(path, "Bash"))[0] is False


@pytest.mark.parametrize(
    ("user", "assistant"),
    [
        ("Please fix PROJ-4521.", "I will inspect, implement, test, and verify PROJ-4521."),
        ("Please inspect PROJ-4521 and PROJ-4242.", "I will inspect both now."),
        ("Before editing, read AGENTS.md.", "I will read it now."),
        (
            "Complete PROJ-4521 and PROJ-4242 before planning; provide the plan afterward.",
            "I will complete the requested work first.",
        ),
    ],
    ids=["one_target", "no_explicit_plan_first_rule", "no_ticket_set", "action_before_planning"],
)
def test_unrelated_contexts_are_allowed(tmp_path: Path, user: str, assistant: str) -> None:
    assert _verdict(_event(_write_transcript(tmp_path, user=user, assistant_text=assistant), "Bash"))[0] is False


class TestInjectedSkillBodiesNeverArmTheGate:
    """A loaded skill body is harness text, not the user's binding request.

    ``skills/code/SKILL.md`` carries a two-ticket, plan-before example, so every
    session that loaded it armed the gate on its own documentation.
    """

    _EXAMPLE = "Two unrelated tickets, the user says fix PROJ-4521 and PROJ-4242 fast: plan first, before any edit."
    _SKILL_BODY = f"Base directory for this skill: /plugins/t3/skills/code\n{_EXAMPLE}"

    def _transcript(self, tmp_path: Path, entry: dict) -> Path:
        path = tmp_path / "transcript.jsonl"
        genuine = {"type": "user", "message": {"role": "user", "content": "Carry on with the refactor."}}
        path.write_text("\n".join(json.dumps(item) for item in (genuine, entry)), encoding="utf-8")
        return path

    def test_a_meta_entry_is_skipped(self, tmp_path: Path) -> None:
        entry = {"type": "user", "isMeta": True, "message": {"role": "user", "content": self._EXAMPLE}}
        assert _verdict(_event(self._transcript(tmp_path, entry), "Bash"))[0] is False

    @pytest.mark.parametrize("wrapper", ["<command-name>/t3:code</command-name>", "<skill-format>true</skill-format>"])
    def test_a_command_or_skill_wrapper_is_skipped(self, tmp_path: Path, wrapper: str) -> None:
        text = f"{wrapper}\n{self._SKILL_BODY}"
        entry = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        assert _verdict(_event(self._transcript(tmp_path, entry), "Bash"))[0] is False

    def test_a_slash_command_with_plan_first_args_arms_the_gate(self, tmp_path: Path) -> None:
        text = f"<command-name>/t3:code</command-name>\n<command-args>{_USER}</command-args>\n{self._SKILL_BODY}"
        entry = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        assert _verdict(_event(self._transcript(tmp_path, entry), "Bash"))[0] is True

    def test_the_same_text_from_the_user_still_arms_the_gate(self, tmp_path: Path) -> None:
        """Anti-vacuity: only the harness wrapper is exempt, not the words."""
        entry = {"type": "user", "message": {"role": "user", "content": self._EXAMPLE}}
        assert _verdict(_event(self._transcript(tmp_path, entry), "Bash"))[0] is True


def test_a_plan_without_verification_does_not_unlock_the_gate(tmp_path: Path) -> None:
    text = "Per-ticket plan: PROJ-4521: implement the literal-gate removal. PROJ-4242: implement the visibility filter."
    assert _verdict(_event(_write_transcript(tmp_path, assistant_text=text), "Bash"))[0] is True


def test_missing_transcript_fails_open(tmp_path: Path) -> None:
    assert _verdict(_event(tmp_path / "missing.jsonl", "Bash"))[0] is False


def test_subagent_call_is_out_of_scope(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path)
    assert _verdict(_event(path, "Bash", agent_id="agent-42"))[0] is False


def test_handler_is_registered_before_other_action_gates() -> None:
    handlers = router._HANDLERS["PreToolUse"]
    assert router.handle_enforce_visible_plan_before_tools in handlers
    visible_plan_index = handlers.index(router.handle_enforce_visible_plan_before_tools)
    edit_gate_index = handlers.index(router.handle_block_edit_before_planned)
    assert visible_plan_index < edit_gate_index


def test_hook_manifest_routes_every_governed_tool_to_pretooluse() -> None:
    manifest = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    routed = {tool for entry in manifest["hooks"]["PreToolUse"] for tool in entry.get("matcher", "").split("|")}
    assert routed >= gate.GATED_TOOLS


def test_scenario_runs_the_shipped_production_hook() -> None:
    scenario_file = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "code.yaml"
    spec = next(spec for spec in load_eval_yaml(scenario_file) if spec.name == "plan_before_any_change_under_load")
    assert spec.production_hooks is True


def test_urgency_scenario_requires_a_plan_in_the_task_or_transcript(tmp_path: Path) -> None:
    scenario_file = (
        Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "instruction_following_under_load.yaml"
    )
    spec = next(spec for spec in load_eval_yaml(scenario_file) if spec.name == "plan_before_change_under_urgency")
    transcript = _write_transcript(tmp_path, user=spec.prompt)

    assert spec.production_hooks is True
    assert _verdict(_event(transcript, "Edit"))[0] is True
    assert _verdict(_event(transcript, "TaskCreate"))[0] is True
    task = _event(transcript, "TaskCreate")
    task["tool_input"] = {
        "subject": "Plan TODO-4 and TODO-6",
        "description": (
            "TODO-4: implement the guarantee matrix and run focused tests. "
            "TODO-6: implement the translations refresh and verify with tests."
        ),
    }
    assert _verdict(task)[0] is False


@pytest.mark.parametrize("tool_name", ["TaskCreate", "TaskUpdate"])
def test_placeholder_task_is_denied_but_plan_bearing_task_is_allowed(tmp_path: Path, tool_name: str) -> None:
    transcript = _write_transcript(tmp_path)
    task = _event(transcript, tool_name)
    task["tool_input"] = {"subject": "Track both tickets", "description": "PROJ-4521 and PROJ-4242"}
    assert _verdict(task)[0] is True
    task["tool_input"] = {"subject": "Plan both tickets", "description": _GOOD_PLAN}
    assert _verdict(task)[0] is False


def test_recorded_plan_task_unlocks_following_edit(tmp_path: Path) -> None:
    transcript = _write_transcript(tmp_path)
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "TaskCreate",
                                "input": {"subject": "Plan both tickets", "description": _GOOD_PLAN},
                            }
                        ],
                    },
                }
            )
        )
    assert _verdict(_event(transcript, "Edit"))[0] is False


def test_read_only_classifier_failure_is_denied(tmp_path: Path) -> None:
    event = _event(_write_transcript(tmp_path), "Bash")
    event["tool_input"] = {"command": "ls"}
    with patch("teatree.hooks.read_only_command.is_read_only", side_effect=ValueError("unknown")):
        assert _verdict(event)[0] is True


def test_unclassifiable_bash_command_is_denied(tmp_path: Path) -> None:
    event = _event(_write_transcript(tmp_path), "Bash")
    event["tool_input"] = {"command": "unknown-command"}
    with patch("teatree.hooks.read_only_command.is_read_only", return_value=None):
        assert _verdict(event)[0] is True


@pytest.mark.parametrize("tool_name", ["Bash", "AskUserQuestion"])
def test_live_router_refuses_before_the_requested_tool_can_execute(tmp_path: Path, tool_name: str) -> None:
    transcript = _write_transcript(tmp_path)
    sentinel = tmp_path / "must-not-exist"
    payload = _event(transcript, tool_name)
    payload["cwd"] = str(tmp_path)
    payload["tool_input"] = (
        {"command": f"touch {sentinel}"}
        if tool_name == "Bash"
        else {"questions": [{"header": "Scope", "question": "Which?", "options": []}]}
    )
    hook = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "hook_router.py"
    result = subprocess.run(
        [sys.executable, str(hook), "--event", "PreToolUse"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "T3_DATA_DIR": str(tmp_path / "state")},
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["hookSpecificOutput"]["gate_id"] == "visible_plan_gate"
    assert not sentinel.exists()


class TestProseTokensNeverArmTheGate:
    """A prose acronym shaped like a ticket id must not arm the gate (lockout regression).

    ``UTF-8``/``ISO-8601``/``SHA-256``/``GPT-4``/``HTTP-404`` all satisfy the old
    uppercase-run-plus-digits shape, so two of them beside the word "plan" armed
    the gate and denied every tool it governs — Read and AskUserQuestion included —
    behind an unlock nobody could write.
    """

    @pytest.mark.parametrize(
        "prose",
        [
            "Decode the payload as UTF-8, normalise every UTF-16 field, and plan that before any edit.",
            "Stamp it in ISO-8601, validate against ISO-4217, and give me the plan before you migrate.",
            "Hash it with SHA-256, keep SHA-512 for the archive, and plan the cutover before shipping.",
            "Compare GPT-4 with GPT-5 on the transcript lane, and plan the run before spending budget.",
            "The proxy answers HTTP-404 where the origin answers HTTP-500; plan the fix before deploying.",
        ],
        ids=["utf_8", "iso_8601", "sha_256", "gpt_4", "http_404"],
    )
    def test_prose_acronyms_do_not_activate_the_gate(self, tmp_path: Path, prose: str) -> None:
        assert _verdict(_event(_write_transcript(tmp_path, user=prose), "Bash"))[0] is False

    def test_real_ticket_ids_still_activate_the_gate(self, tmp_path: Path) -> None:
        assert _verdict(_event(_write_transcript(tmp_path), "Bash"))[0] is True

    def test_ticket_ids_without_a_plan_first_rule_stay_inert(self, tmp_path: Path) -> None:
        user = "Handle PROJ-4521 and PROJ-4242 today."
        assert _verdict(_event(_write_transcript(tmp_path, user=user), "Bash"))[0] is False

    def test_a_single_prose_token_stays_inert(self, tmp_path: Path) -> None:
        user = "Decode it as UTF-8, and plan the change before you touch anything."
        assert _verdict(_event(_write_transcript(tmp_path, user=user), "Bash"))[0] is False


class TestNeverLockoutEscapes:
    """The gate honours the three-escape contract `hooks/CLAUDE.md` requires.

    It governs Read and AskUserQuestion, so an armed false positive removed
    every way out at once: there was no kill-switch key, no per-call token,
    and — the deny prefix being absent from the breaker's UX allow-list — a
    retry escalated instead of failing open.
    """

    def test_per_call_token_allows_the_governed_call(self, tmp_path: Path) -> None:
        event = _event(_write_transcript(tmp_path), "Bash")
        event["tool_input"] = {"command": "touch notes.md  # [visible-plan-ok: scratch file the owner asked for]"}

        assert _verdict(event)[0] is False

    def test_a_token_with_an_empty_reason_does_not_unlock(self, tmp_path: Path) -> None:
        event = _event(_write_transcript(tmp_path), "Bash")
        event["tool_input"] = {"command": "touch notes.md  # [visible-plan-ok: ]"}

        assert _verdict(event)[0] is True

    def test_a_token_with_an_empty_reason_on_read_only_bash_does_not_unlock(self, tmp_path: Path) -> None:
        event = _event(_write_transcript(tmp_path), "Bash")
        event["tool_input"] = {"command": "ls # [visible-plan-ok: ]"}
        assert gate.visible_plan_ok_token(event) is None
        assert _verdict(event)[0] is False

    def test_kill_switch_disables_the_gate(self, tmp_path: Path) -> None:
        with patch.object(gate, "_gate_enabled", lambda: False):
            assert _verdict(_event(_write_transcript(tmp_path), "Bash"))[0] is False

    def test_deny_reason_is_on_the_breaker_ux_allow_list(self, tmp_path: Path) -> None:
        payload = _verdict(_event(_write_transcript(tmp_path), "Bash"))[1]

        assert payload is not None
        assert deny_circuit_breaker.deny_is_ux_gate(payload["hookSpecificOutput"]["permissionDecisionReason"]) is True

    def test_the_escape_token_is_stripped_from_the_deny_fingerprint(self) -> None:
        assert deny_circuit_breaker._SIGNATURE_STRIP_RE.sub("", "ls [visible-plan-ok: why]").strip() == "ls"

    def test_the_kill_switch_key_is_a_registered_cold_hook_setting(self) -> None:
        assert COLD_HOOK_SETTINGS["visible_plan_gate_enabled"].default is True

    def test_the_self_rescue_disable_command_is_never_denied(self) -> None:
        assert is_self_rescue("t3 t3-teatree gate visible-plan disable") is True


class TestPlanShapesTheSkillsMandate:
    """A correct plan unlocks the gate whatever order its clauses run in.

    The coverage check read only the text AFTER a target and demanded the
    verification verb come after the implementation one, so an id trailing its
    actions and the verify-then-implement order `skills/code/SKILL.md` mandates
    for TDD were both denied.
    """

    _TRAILING_IDS = (
        "Per-ticket plans, before any action:\n"
        "Inspect forms, implement the literal-gate removal and run the focused tests — PROJ-4521.\n"
        "Inspect views, implement the visibility filter and run the two-user tests — PROJ-4242."
    )
    _TDD_ORDER = (
        "Per-ticket plans, before any action:\n"
        "PROJ-4521: write the failing test first, run it red, then implement the literal-gate removal.\n"
        "PROJ-4242: write the failing two-user test, run it red, then implement the visibility filter."
    )

    @pytest.mark.parametrize("plan", [_TRAILING_IDS, _TDD_ORDER], ids=["ids_trail_actions", "verify_then_implement"])
    def test_a_correct_plan_unlocks_the_gate(self, tmp_path: Path, plan: str) -> None:
        assert _verdict(_event(_write_transcript(tmp_path, assistant_text=plan), "Bash"))[0] is False

    @pytest.mark.parametrize("tool_name", ["Read", "Grep", "Glob"])
    def test_read_only_planning_tools_are_never_gated(self, tmp_path: Path, tool_name: str) -> None:
        assert tool_name not in gate.GATED_TOOLS
        assert _verdict(_event(_write_transcript(tmp_path), tool_name))[0] is False


_ROUTER = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "hook_router.py"
_ORDER = "Deliver PROJ-601 and PROJ-602. Binding rule: present a per-story plan before any edit, commit, or push."
_STATUS_QUESTION = "what is the current status?"
_REPRO_READ = (
    "cd ~/.claude/skills/project-status && wc -l references/current-state.md "
    "&& sed -n '1,60p' references/current-state.md && git log -1"
)
_EDIT = {"file_path": "/tmp/visible-plan/notes.md", "old_string": "a", "new_string": "b"}


def _user_entry(text: str, **flags: object) -> dict:
    return {"type": "user", **flags, "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant_entry(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_result_entry(content: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": content}]},
    }


def _skill_load(body: str) -> list[dict]:
    return [
        _tool_result_entry("Launching skill: project-status"),
        _user_entry(f"Base directory for this skill: /skills/project-status\n\n{body}", isMeta=True),
    ]


def _refused_by_plan_gate(tmp_path: Path, entries: list[dict], tool_name: str, tool_input: dict) -> bool:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
    payload = {
        "session_id": f"sess-{tmp_path.name}",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    result = subprocess.run(
        [sys.executable, str(_ROUTER), "--event", "PreToolUse"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={
            **os.environ,
            "T3_DATA_DIR": str(tmp_path / "state"),
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(tmp_path / "hook-state"),
        },
    )
    return "visible_plan_gate" in result.stdout


class TestOnlyTheOwnersOrderArmsTheGate:
    @pytest.mark.parametrize(
        "entries",
        [
            [_user_entry(_STATUS_QUESTION, origin={"kind": "human"}), *_skill_load(_ORDER)],
            [_user_entry(f"{_STATUS_QUESTION}\n<system-reminder>{_ORDER}</system-reminder>", origin={"kind": "human"})],
            [
                _user_entry(_STATUS_QUESTION, origin={"kind": "human"}),
                _user_entry(
                    f"<task-notification><result>{_ORDER}</result></task-notification>",
                    origin={"kind": "task-notification"},
                ),
            ],
            [_user_entry(f"This session is being continued from a previous session. {_ORDER}", isCompactSummary=True)],
        ],
        ids=["skill_body", "system_reminder", "agent_output", "compaction_summary"],
    )
    def test_targets_outside_the_owners_prompt_never_arm(self, tmp_path: Path, entries: list[dict]) -> None:
        assert _refused_by_plan_gate(tmp_path, entries, "Edit", _EDIT) is False

    @pytest.mark.parametrize(
        "prompt",
        [
            "What is the current status of PROJ-601 and PROJ-602? Was a plan presented before any edit?",
            "Give me the status of PROJ-601 and PROJ-602, with a breakdown before the standup.",
            "Report whether the plan before editing PROJ-601 and PROJ-602 was approved.",
            "Update me on PROJ-601 and PROJ-602. Report whether the plan before editing was approved.",
            "Give me an update on PROJ-601 and PROJ-602. Report whether the plan before editing was approved.",
            "Summarise the deploy of PROJ-601 and PROJ-602. Report whether the plan before editing was approved.",
        ],
        ids=[
            "status_question",
            "status_report_request",
            "report_on_a_past_plan",
            "update_me_and_past_plan",
            "an_update_and_past_plan",
            "deploy_summary_and_past_plan",
        ],
    )
    def test_a_status_or_read_request_never_arms(self, tmp_path: Path, prompt: str) -> None:
        entries = [_user_entry(prompt, origin={"kind": "human"})]

        assert _refused_by_plan_gate(tmp_path, entries, "Agent", {"prompt": "Summarise the status"}) is False

    def test_classified_read_only_bash_is_open(self, tmp_path: Path) -> None:
        entries = [_user_entry(_ORDER, origin={"kind": "human"})]
        assert _refused_by_plan_gate(tmp_path, entries, "Bash", {"command": _REPRO_READ}) is False

    @pytest.mark.parametrize("tool_name", ["TaskCreate", "TaskUpdate"])
    def test_task_bookkeeping_must_record_both_plans(self, tmp_path: Path, tool_name: str) -> None:
        entries = [_user_entry(_ORDER, origin={"kind": "human"})]
        assert _refused_by_plan_gate(tmp_path, entries, tool_name, {"subject": "Track both tickets"}) is True
        plan = (
            "PROJ-601: implement the first change and run focused tests. "
            "PROJ-602: implement the second change and verify with tests."
        )
        assert (
            _refused_by_plan_gate(tmp_path, entries, tool_name, {"subject": "Plan both tickets", "description": plan})
            is False
        )

    @pytest.mark.parametrize(
        ("entries", "tool_name", "tool_input"),
        [
            ([_user_entry(_ORDER, origin={"kind": "human"})], "Edit", _EDIT),
            ([_user_entry(_ORDER, origin={"kind": "human"})], "Bash", {"command": "git commit -m wip"}),
            ([_user_entry(_ORDER, origin={"kind": "human"})], "Bash", {"command": "sed -n '1w out.txt' in.txt"}),
            ([_user_entry(_ORDER, origin={"kind": "human"})], "Bash", {"command": "find . -fprintf out.txt '%p\\n'"}),
            ([_user_entry(_ORDER, origin={"kind": "human"}), *_skill_load("No targets here.")], "Edit", _EDIT),
        ],
        ids=["edit", "committing_bash", "sed_script_write", "find_fprintf", "order_then_skill_load"],
    )
    def test_an_explicit_order_naming_targets_still_blocks_a_write(
        self, tmp_path: Path, entries: list[dict], tool_name: str, tool_input: dict
    ) -> None:
        assert _refused_by_plan_gate(tmp_path, entries, tool_name, tool_input) is True

    def test_one_sentence_planning_the_targets_before_their_fix_blocks_a_write(self, tmp_path: Path) -> None:
        entries = [_user_entry("Plan PROJ-1 and PROJ-2 before you fix them.", origin={"kind": "human"})]

        assert _refused_by_plan_gate(tmp_path, entries, "Edit", _EDIT) is True

    def test_a_plan_written_before_a_skill_load_still_counts(self, tmp_path: Path) -> None:
        plan = (
            "Plan — PROJ-601: implement the export fix, then run its tests and verify.\n"
            "Plan — PROJ-602: implement the import fix, then run its tests and verify."
        )
        entries = [_user_entry(_ORDER, origin={"kind": "human"}), _assistant_entry(plan), *_skill_load("No targets.")]

        assert _refused_by_plan_gate(tmp_path, entries, "Edit", _EDIT) is False
