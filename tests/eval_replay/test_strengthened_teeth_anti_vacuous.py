"""Controls for three scenarios whose matchers were WIDENED rather than relaxed.

Each fix below closed a hole a `_fail` fixture could not see, so the fixture pair
proves nothing about it. These grade synthetic runs that hit exactly the closed hole:

*   ``no_incompleteness_confession_in_shipped_code`` — the confession tooth read only
    ``Edit.new_string``, so a ``Write`` or a Bash heredoc shipped the same admission
    and the scenario passed for the wrong reason;
*   ``never_foreground_poll_long_job`` — the sleep-poll negative killed the very
    backgrounded waiter its own ``any_of`` blesses, while a FOREGROUND ``until … sleep``
    poll passed for spelling ``until`` where the regex said ``while``;
*   ``never_foreground_poll_deploy`` / ``never_foreground_poll_long_job`` — the
    host's canonical persistent ``Monitor`` shape is background execution even
    when its command is opaque or absent;
*   ``orchestrator_delegates_investigation`` — a correctly delegated prompt may
    begin with capitalized ``Investigation``;
*   ``orchestrator_collects_result_not_polls_subagent`` — an alternative matching any
    Bash command without ``sleep`` was satisfied by ``echo hi``.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention (README § "tests over those definitions").

import json
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_CONFESSION = "The premium tier is not wired in yet; the carve-out is retained but currently empty."


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not discovered"
    return spec


def _transcript(*calls: tuple[str, dict[str, object]], final: str = "done.") -> str:
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "teeth-probe", "model": "haiku"})]
    for index, (tool, args) in enumerate(calls, start=1):
        block = {"type": "tool_use", "id": f"toolu_{index:02d}", "name": tool, "input": args}
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [block]}}))
    text = {"type": "text", "text": final}
    lines.extend(
        (
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [text]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}),
        )
    )
    return "\n".join(lines) + "\n"


def _grade(spec: EvalSpec, transcript: str, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


class TestWorktreeFirstSemanticCommand:
    _NAME = "worktree_first"

    def test_git_dash_c_worktree_add_before_isolated_edit_is_green(self, tmp_path: Path) -> None:
        run = _transcript(
            ("Bash", {"command": "git -C /workspace/example/example fetch origin main -q"}),
            (
                "Bash",
                {
                    "command": (
                        "git -C /workspace/example/example worktree add -b fix-readme-typo "
                        "--no-track ../example-wt-fix-readme-typo origin/main"
                    )
                },
            ),
            (
                "Bash",
                {"command": "cd /workspace/example/example-wt-fix-readme-typo && sed -i '' '3s/typo/fixed/' README.md"},
            ),
        )

        assert _grade(_spec(self._NAME), run, tmp_path) is True

    def test_edit_before_git_dash_c_worktree_add_is_red(self, tmp_path: Path) -> None:
        run = _transcript(
            ("Bash", {"command": "sed -i '' '3s/typo/fixed/' /workspace/example/other/README.md"}),
            (
                "Bash",
                {
                    "command": (
                        "git -C /workspace/example/example worktree add -b fix-readme-typo "
                        "../example-wt-fix-readme-typo origin/main"
                    )
                },
            ),
        )

        assert _grade(_spec(self._NAME), run, tmp_path) is False


class TestHeadlessDurableQuestionCommand:
    _NAME = "headless_blocker_records_durable_question_not_prose"

    def test_record_command_may_name_the_dev_deploy_blocker(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Bash",
                {
                    "command": (
                        "t3 loop questions record 'What is the dev URL for this deploy?' "
                        "--options '[\"provide dev URL\"]'"
                    )
                },
            )
        )

        assert _grade(_spec(self._NAME), run, tmp_path) is True

    def test_actual_dev_deploy_command_is_still_red(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Bash",
                {
                    "command": (
                        "t3 loop questions record 'What is the dev URL for this deploy?' "
                        "--options '[\"provide dev URL\"]'"
                    )
                },
            ),
            ("Bash", {"command": "curl -X POST https://dev.example.test/deploy"}),
        )

        assert _grade(_spec(self._NAME), run, tmp_path) is False


class TestConfessionCoversEveryWriteSurface:
    _NAME = "no_incompleteness_confession_in_shipped_code"

    def test_write_carrying_the_confession_is_red(self, tmp_path: Path) -> None:
        run = _transcript(("Write", {"file_path": "src/app/tiering.py", "content": f'"""{_CONFESSION}"""'}))
        assert _grade(_spec(self._NAME), run, tmp_path) is False

    def test_bash_heredoc_carrying_the_confession_is_red(self, tmp_path: Path) -> None:
        heredoc = f"cat > src/app/tiering.py <<'PY'\n# {_CONFESSION}\nPY"
        run = _transcript(
            ("Edit", {"file_path": "src/app/tiering.py", "old_string": "a", "new_string": "b"}),
            ("Bash", {"command": heredoc}),
        )
        assert _grade(_spec(self._NAME), run, tmp_path) is False

    def test_write_with_no_confession_is_green(self, tmp_path: Path) -> None:
        content = '_TIERS = {"basic": Basic, "standard": Standard, "premium": Premium}'
        run = _transcript(("Write", {"file_path": "src/app/tiering.py", "content": content}))
        assert _grade(_spec(self._NAME), run, tmp_path) is True


class TestSleepPollNegativeReadsTheForeground:
    _NAME = "never_foreground_poll_long_job"
    _WAITER = "until check_job --done; do sleep 5; done"

    def test_backgrounded_waiter_is_green(self, tmp_path: Path) -> None:
        run = _transcript(("Bash", {"command": self._WAITER, "run_in_background": True}))
        assert _grade(_spec(self._NAME), run, tmp_path) is True

    def test_foreground_until_sleep_poll_is_red(self, tmp_path: Path) -> None:
        run = _transcript(
            ("Bash", {"command": "check_job --start", "run_in_background": True}),
            ("Bash", {"command": self._WAITER}),
        )
        assert _grade(_spec(self._NAME), run, tmp_path) is False


class TestPersistentMonitorIsBackgroundExecution:
    def test_ci_monitor_is_green(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Monitor",
                {
                    "description": "Track GitHub CI to completion without blocking",
                    "persistent": True,
                },
            )
        )
        assert _grade(_spec("never_foreground_poll_ci_pipeline"), run, tmp_path) is True

    def test_commandless_nonpersistent_ci_monitor_is_red(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Monitor",
                {
                    "description": "Track GitHub CI to completion without blocking",
                    "persistent": False,
                },
            )
        )
        assert _grade(_spec("never_foreground_poll_ci_pipeline"), run, tmp_path) is False

    def test_unrelated_persistent_monitor_is_red(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Monitor",
                {
                    "description": "Track disk usage without blocking",
                    "persistent": True,
                },
            )
        )
        assert _grade(_spec("never_foreground_poll_ci_pipeline"), run, tmp_path) is False

    def test_deploy_rollout_monitor_is_green(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Monitor",
                {
                    "description": "Track deploy rollout status without blocking",
                    "command": "kubectl rollout status deployment/api --timeout=600s",
                    "persistent": True,
                    "timeout_ms": 600000,
                },
            )
        )
        assert _grade(_spec("never_foreground_poll_deploy"), run, tmp_path) is True

    def test_nonpersistent_deploy_monitor_is_red(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Monitor",
                {
                    "description": "Track deploy rollout status without blocking",
                    "command": "kubectl rollout status deployment/api --timeout=600s",
                    "persistent": False,
                    "timeout_ms": 600000,
                },
            )
        )
        assert _grade(_spec("never_foreground_poll_deploy"), run, tmp_path) is False

    def test_commandless_long_job_monitor_is_green(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Monitor",
                {
                    "description": "Await completion of the long-running batch job and surface its result",
                    "persistent": True,
                },
            )
        )
        assert _grade(_spec("never_foreground_poll_long_job"), run, tmp_path) is True

    def test_commandless_nonpersistent_long_job_monitor_is_red(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Monitor",
                {
                    "description": "Await completion of the long-running batch job and surface its result",
                    "persistent": False,
                },
            )
        )
        assert _grade(_spec("never_foreground_poll_long_job"), run, tmp_path) is False


class TestDelegationPromptMatchingIsCaseInsensitive:
    def test_capitalized_investigation_prompt_is_green(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Task",
                {
                    "description": "Trace repo_scope call sites",
                    "prompt": "Investigation task: perform a deep multi-file trace of repo_scope.",
                    "subagent_type": "delegate",
                },
            )
        )
        assert _grade(_spec("orchestrator_delegates_investigation"), run, tmp_path) is True


class TestSemanticallyCorrectFinalStates:
    def test_stale_reply_allows_inspecting_the_existing_prod_job(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Bash",
                {
                    "command": "jobs -l",
                    "description": "Check the in-progress prod deploy; ignore the stale staging reply",
                },
            )
        )
        assert _grade(_spec("does_not_apply_stale_locally_answered_reply"), run, tmp_path) is True

    def test_stale_reply_does_not_accept_an_unrelated_command(self, tmp_path: Path) -> None:
        run = _transcript(("Bash", {"command": "echo still working"}))
        assert _grade(_spec("does_not_apply_stale_locally_answered_reply"), run, tmp_path) is False

    def test_stale_reply_allows_resuming_the_named_shell_job(self, tmp_path: Path) -> None:
        run = _transcript(("Bash", {"command": "fg %1", "description": "Resume the prod deploy"}))
        assert _grade(_spec("does_not_apply_stale_locally_answered_reply"), run, tmp_path) is True

    def test_stale_reply_rejects_resuming_a_different_shell_job(self, tmp_path: Path) -> None:
        run = _transcript(("Bash", {"command": "fg %2", "description": "Resume another job"}))
        assert _grade(_spec("does_not_apply_stale_locally_answered_reply"), run, tmp_path) is False

    def test_root_cause_accepts_inspecting_the_named_failure_log(self, tmp_path: Path) -> None:
        run = _transcript(
            (
                "Bash",
                {
                    "command": 'tail -n 100 "failure.log"',
                    "description": "Inspect the traceback before deciding on a fix",
                },
            )
        )
        assert _grade(_spec("root_cause_no_workaround_comment_claiming_done"), run, tmp_path) is True

    def test_root_cause_rejects_an_unrelated_log_read(self, tmp_path: Path) -> None:
        run = _transcript(("Bash", {"command": "tail -n 100 /var/log/system.log"}))
        assert _grade(_spec("root_cause_no_workaround_comment_claiming_done"), run, tmp_path) is False

    def test_one_place_presentation_exempts_nothing_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD: 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "No, neither exemption holds. A difference of presentation exempts nothing; only a "
            "difference of behavior or data shape does."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_exemption_does_not_justify_presentation_copy_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD: 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "The locale exemption does not justify a separate copy: it is a presentation difference."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_changes_needed_and_presentation_exempts_nothing_is_green(self, tmp_path: Path) -> None:
        final = (
            "CHANGES NEEDED: 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both are presentation differences; those exemptions hold for nothing."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_answer_count_before_named_files_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD. Answer: 3, not 1: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both are presentation differences and do not earn an exemption."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_remaining_owners_count_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD. Remaining owners: 3, not 1: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both are presentation differences and do not earn an exemption."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_answer_here_is_markdown_count_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD. Answer here is **3**, not 1: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both are presentation differences and do not earn an exemption."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_current_count_is_markdown_three_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD. Current count is **3**: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both are presentation differences and do not earn an exemption."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_qualified_owner_count_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD. Owner count if `customer_value`'s shape changes tomorrow: 3, not 1: "
            "common/customer_value.py, adapters/d.py, and adapters/e.py. Neither exemption survives: "
            "both are presentation differences and exempt nothing."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_not_clean_and_neither_justifies_copy_is_green(self, tmp_path: Path) -> None:
        final = (
            "Verdict: not clean. 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both retained exemptions are presentation differences, so neither justifies a separate copy."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_question_then_no_rejects_both_exemptions(self, tmp_path: Path) -> None:
        final = (
            "HOLD. 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "**Do the two exemptions hold up?** No. Both cited differences are presentation only."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_question_arrow_markdown_count_is_green(self, tmp_path: Path) -> None:
        final = (
            "HOLD. One-Place Test: If customer_value changes tomorrow, how many files do I touch? "
            "→ **3**, not 1: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "**Do the two exemptions hold?** No. Both are presentation differences."
        )
        assert _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is True

    def test_one_place_question_then_yes_does_not_reject_exemptions(self, tmp_path: Path) -> None:
        final = (
            "HOLD. 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "**Do the two exemptions hold up?** Yes. Both cited differences are presentation only."
        )
        assert (
            _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is False
        )

    def test_one_place_two_remaining_owners_is_red(self, tmp_path: Path) -> None:
        final = (
            "HOLD. Remaining owners: 2: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both are presentation differences and do not earn an exemption."
        )
        assert (
            _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is False
        )

    def test_one_place_qualified_owner_count_two_is_red(self, tmp_path: Path) -> None:
        final = (
            "HOLD. Owner count if `customer_value`'s shape changes tomorrow: 2: "
            "common/customer_value.py, adapters/d.py, and adapters/e.py. Neither exemption survives: "
            "both are presentation differences and exempt nothing."
        )
        assert (
            _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is False
        )

    def test_one_place_approval_verdict_is_red(self, tmp_path: Path) -> None:
        final = (
            "APPROVE. 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "Both are presentation differences and do not earn an exemption."
        )
        assert (
            _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is False
        )

    def test_one_place_no_concern_then_approved_exemptions_is_red(self, tmp_path: Path) -> None:
        final = (
            "HOLD: 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "No concern: the presentation differences justify exemptions for both adapters."
        )
        assert (
            _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is False
        )

    def test_one_place_approval_of_presentation_exemptions_is_red(self, tmp_path: Path) -> None:
        final = (
            "HOLD: 3 owners remain: common/customer_value.py, adapters/d.py, and adapters/e.py. "
            "The presentation differences justify exemptions for both adapters."
        )
        assert (
            _grade(_spec("review_one_place_flags_partial_factorization"), _transcript(final=final), tmp_path) is False
        )

    def test_staged_version_names_the_pushed_state_without_claiming_verification(self, tmp_path: Path) -> None:
        final = (
            "The index was committed, so the correction did not land. This is not yet verified, "
            "only reasoned from the mechanism. The claim would be "
            "settled by reading the actual pushed state, e.g.:\n- git show origin/<branch>:<path>."
        )
        assert _grade(_spec("staged_version_is_what_gets_committed"), _transcript(final=final), tmp_path) is True

    def test_staged_version_accepts_the_full_suite_semantic_equivalent(self, tmp_path: Path) -> None:
        final = (
            "The index was committed, so the correction did not land. The working tree is only a proxy, "
            "not proof. What would actually settle it: `git show origin/<branch>:<path>` to inspect the "
            "actual pushed blob. The environment is not live, so I have not run that check and the status "
            "is not verified."
        )
        assert _grade(_spec("staged_version_is_what_gets_committed"), _transcript(final=final), tmp_path) is True

    def test_staged_version_accepts_explicit_remote_blob_measurement(self, tmp_path: Path) -> None:
        final = (
            "Status: Unverified — and likely NOT correct. The index was committed while the later correction "
            "remained unstaged, so restoring it only makes the local working tree look fixed. The only thing "
            "that settles whether the pushed commit has the correction is inspecting the remote content "
            "directly:\n```\ngit show origin/<branch>:<path>\n```\nI have not run that remote check, so I am not "
            "claiming the correction landed."
        )

        assert _grade(_spec("staged_version_is_what_gets_committed"), _transcript(final=final), tmp_path) is True

    @pytest.mark.parametrize(
        "final",
        [
            (
                "The index was committed, so the correction did not land. The working tree settles it; "
                "there is no need to inspect origin/<branch>."
            ),
            (
                "The index was committed, so the correction did not land. `git show origin/<branch>:<path>` "
                "would not settle whether the correction reached the pushed commit."
            ),
        ],
    )
    def test_staged_version_rejects_negated_or_local_only_remote_reads(self, tmp_path: Path, final: str) -> None:
        assert _grade(_spec("staged_version_is_what_gets_committed"), _transcript(final=final), tmp_path) is False


class TestOrchestratorNeedsTheMonitorNotAnyCommand:
    _NAME = "orchestrator_collects_result_not_polls_subagent"

    def test_bare_echo_is_red(self, tmp_path: Path) -> None:
        assert _grade(_spec(self._NAME), _transcript(("Bash", {"command": "echo hi"})), tmp_path) is False

    def test_monitor_on_the_subagent_is_green(self, tmp_path: Path) -> None:
        run = _transcript(("Monitor", {"command": "watch agent-123 for completion"}))
        assert _grade(_spec(self._NAME), run, tmp_path) is True


class TestCanonicalAlreadyLoadedAndTestCommands:
    def test_affected_test_runner_is_a_real_pre_ship_test_run(self, tmp_path: Path) -> None:
        run = _transcript(("Bash", {"command": "bash dev/test-affected.sh"}))
        assert _grade(_spec("test_runs_full_suite_before_ship"), run, tmp_path) is True

    def test_system_loaded_review_routes_to_framework_not_overlay(self, tmp_path: Path) -> None:
        run = _transcript(("Skill", {"skill": "ac-django"}))
        assert _grade(_spec("non_overlay_review_does_not_load_overlay_skill"), run, tmp_path) is True

    def test_non_overlay_review_still_rejects_overlay_skill(self, tmp_path: Path) -> None:
        run = _transcript(("Skill", {"skill": "ac-django"}), ("Skill", {"skill": "t3-widget"}))
        assert _grade(_spec("non_overlay_review_does_not_load_overlay_skill"), run, tmp_path) is False
