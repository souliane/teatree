# Ticket thread delivery recovery implementation plan

**Goal:** Keep each Slack direction on its original task across sender failure and compaction.

**Architecture:** `SessionCommand` remains the durable command identity and task binding. The Slack loop reports an unsettled command as uncertain when its live session disappears. Each compaction lane preserves operator input: Pydantic keeps both message forms verbatim, and a known-window Claude child gets its own explicit compaction settings.

**Tech Stack:** Django, Pydantic AI, Claude Agent SDK, pytest.

## 1. Session loss after an unsettled send

- Change `tests/teatree_loop/slack_answer/test_session_routing.py`: model acceptance followed by a sender crash and a missing session; assert one `unknown_delivery` receipt, the original `SessionCommand.task_id`, and no new task on repeated cycles.
- Run that test against the current code to observe reassignment, then change `src/teatree/loop/slack_answer/session_routing.py` to settle uncertainty on the original command. Keep the existing same-session retry, mismatch refusal, and ordinary no-session fallback tests green.

## 2. Pydantic compaction

- Change `tests/teatree_agents/lane_b/test_compaction.py`: place an idle `[TeaTree operator follow-up id=...]` message in stale history and assert it survives verbatim. Observe the pre-fix failure.
- Change `src/teatree/agents/lane_b/compaction.py` to pin both existing operator message forms without changing tool-pair or recent-history behavior; rerun the focused tests.

## 3. Claude native compaction

- Change `tests/teatree_agents/test_t3_teatree_skill_dispatch.py`: prepare a known-window Claude child with inherited disable switches and stale window/threshold, and assert compaction is enabled with the selected window and reserve. Observe the pre-fix failure.
- Change `src/teatree/agents/runner_preparation.py` to remove inherited disable switches and replace stale values only for the known-window Claude child. Rerun the focused tests.

## 4. Live-system measurement and verification

- Coordinator: run a real `claude_sdk` child with an explicit small `agent_model_context_windows` value and an operator direction before the compaction threshold. **Named measurement:** capture the child's `compact_boundary` event count and trigger, its observed context usage immediately before the boundary, and whether its post-boundary reply restates the operator direction. A zero event count or a missing direction fails the experiment. The coordinator also owns the real Slack-thread delivery proof.
- Builder: run `bash dev/test-affected.sh`, commit, then run `t3 tool verify-gates --expect-sha <committed HEAD>` and record both measured SHA and exit code. The coordinator records the live measurements and required CI at the pushed head without opening another PR.

## 5. Independent review findings

- Push brief: retain the newest claimed coding or debugging task's constraints; otherwise use the newest completed task. Pending and failed tasks cannot constrain the push. Test each state and claimed precedence through `_brief_for_worktree`.
- Compaction: assert the reduced history stays below the measured bound on the tool-pair test; remove the skip-forward loop temporarily to prove the assertion fails.
- Unsettled command: test that a changed ticket or text is rejected without settling or dispatching; replace the mismatch condition temporarily to prove the test fails.
- Bound question answer: the recorded plan's D5 requires the thread to say when the ended task resumes. Record a settled `SessionCommand` for the notice, confirm it in the thread, and replay it if Slack read-back fails. Assert both delivery and retry through the public cycle.

## Open questions & assumptions

- Assumed: an unsettled send with a missing session is ambiguous; `unknown_delivery` is the honest receipt and the original task owns replay.
- Decided-by-user: the coordinator owns live Claude and Slack experiments and their recorded results.
- Assumed: a failed task is the terminal cancelled case because Task.Status has no cancelled member.
- Assumed: the push of this branch's migration was blocked by a queued future rework brief, so the push brief selection is included even though the original plan did not name it.
