"""The half of the roll whose keys gated, timed, or bounded one loop or scanner unit.

Each was a second answer to a question the loop layer already answers — the active
preset admits or refuses the unit, its ``Loop`` row carries the cadence, and its own
scanner field carries the per-tick bound. Retirements whose job moved anywhere else
— onto a constant, an env var, another key, or a subsystem that went with it — stay
in :mod:`teatree.config.retired_settings_ledger`, which appends this roll to its own.
"""

from teatree.config.retirement_record import RetiredSetting

LOOP_OWNED_RETIREMENTS: tuple[RetiredSetting, ...] = (
    RetiredSetting(
        key="dream_promotion_cap",
        reason="a dream pass batches every promotion into ONE ticket, so there is nothing left to ration",
    ),
    RetiredSetting(
        key="db_backup_cadence_hours",
        reason=(
            "the Loop row's daily_at anchor IS the cadence; a second daily clock in series "
            "pushed the backup later every day instead of keeping it daily"
        ),
    ),
    RetiredSetting(
        key="loop_runner_enabled",
        reason=(
            "it was a second stop surface that never stopped the fleet — the reactive queue drain ran "
            "while it was off and it never reached an in-flight sub-agent. What stops the fleet is the "
            'active preset admitting zero loops: `t3 loop preset use off --reason "<why>"`'
        ),
    ),
    RetiredSetting(
        key="backlog_sweep_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'backlog_sweep' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="db_backup_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'db_backup' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="dogfood_smoke_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'dogfood' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="eval_local_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'eval_local' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="idle_stack_reaper_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'idle_stack_reaper' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="local_stack_queue_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'local_stack_queue' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="resource_pressure_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'resource_pressure' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="scanning_news_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'news' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="snapshot_warmer_disabled",
        reason=(
            "a preset holds a total opinion on every loop, so a scalar gating the whole of one "
            "loop's only job was a second answer to a question already answered — admit or refuse "
            "the 'snapshot_warmer' loop in the active preset"
        ),
    ),
    RetiredSetting(
        key="eval_local_cadence_hours",
        reason=(
            "the eval_local Loop row's delay_seconds IS the cadence; a weekly gate checked from a "
            "daily row is a second clock in series, which fires late rather than weekly"
        ),
    ),
    RetiredSetting(
        key="triage_assessor_cadence_hours",
        reason=(
            "the triage_assessor Loop row's delay_seconds IS the cadence; a daily gate checked from "
            "an hourly row is a second clock in series, which fires late rather than daily"
        ),
    ),
    RetiredSetting(
        key="issue_implementer_enabled",
        reason=(
            "a preset holds a total opinion on every loop, so a per-loop existence scalar was a "
            "second answer to a question already answered — admit or refuse the loop in the "
            "active preset"
        ),
    ),
    RetiredSetting(
        key="triage_assessor_enabled",
        reason=(
            "a preset holds a total opinion on every loop, so a per-loop existence scalar was a "
            "second answer to a question already answered — admit or refuse the loop in the "
            "active preset"
        ),
    ),
    RetiredSetting(
        key="todo_sweep_disabled",
        replacement="task_sweep_disabled",
        reason="the loop unit reconciles teatree Task rows, not the harness TODO list (#129)",
    ),
    RetiredSetting(
        key="todo_sweep_recheck_interval_hours",
        replacement="task_sweep_recheck_interval_hours",
        reason="the loop unit reconciles teatree Task rows, not the harness TODO list (#129)",
    ),
    RetiredSetting(
        key="architectural_review_retry_backoff_hours",
        reason="a loop's cadence IS its retry interval; arch_review checks daily, so a failure retries next day",
    ),
    RetiredSetting(
        key="architectural_review_disabled",
        reason="the arch_review Loop row (and any preset masking it) decides whether the review runs",
    ),
    RetiredSetting(
        key="issue_implementer_cadence_hours",
        reason="the issue-implementer cadence is its `Loop` row's `delay_seconds`, which no setting fed (#4203)",
    ),
    RetiredSetting(
        key="issue_implementer_require_label",
        reason=(
            "admission is decided by the `decide_intake` table, which admits a trusted author "
            "with no label at all; the untrusted-author case uses `issue_implementer_label` (#3634)"
        ),
    ),
    RetiredSetting(
        key="idle_stack_reaper_cadence_minutes",
        reason=(
            "the idle-stack reaper's debounce is the scanner's own cadence field; nothing has ever "
            "varied it, and a knob whose only effect is to move a 5-minute marker gate is not a policy"
        ),
    ),
    RetiredSetting(
        key="resource_pressure_cadence_minutes",
        reason=(
            "the resource_pressure Loop row's delay_seconds is the tick; this was only the marker "
            "debounce between measurements, and it is each scanner's own cadence field now"
        ),
    ),
    RetiredSetting(
        key="resource_pressure_min_free_interval_minutes",
        reason=(
            "the anti-thrash floor between freeing passes is the scanner's own "
            "min_free_interval_minutes field; no box has ever set it"
        ),
    ),
    RetiredSetting(
        key="max_worktree_gc_per_tick",
        reason=(
            "the per-tick cap is protocol, not policy — it bounds ONE pass of a heuristic sweep "
            "that reports and defers the rest to the next tick, so it is a constant in "
            "`loop/worktree_gc.py`. What decides whether the GC removes anything at all is "
            "`allow_destructive_disk`"
        ),
    ),
    RetiredSetting(
        key="local_stack_queue_max_attempts",
        reason=(
            "the retry-ladder length is protocol, not policy — 13 is the Fibonacci-minute backoff "
            "walked to its end, so it is a constant in `core/models/local_stack_queue.py`. What "
            "decides whether a request queues at all is `max_concurrent_local_stacks`"
        ),
    ),
    RetiredSetting(
        key="triage_assessor_max_issues_per_tick",
        reason=(
            "the per-tick batch bound is the scanner's own protocol — "
            "`TriageAssessorScanner.max_issues_per_tick` (10) holds it, keeping a queued task's "
            "serialized list reviewable; whether the pass runs at all is the triage_assessor Loop "
            "row and the active preset"
        ),
    ),
    RetiredSetting(
        key="mr_triage_max_mrs_per_tick",
        reason=(
            "the per-tick survey bound is the scanner's own protocol — "
            "`MrTriageScanner.max_mrs_per_tick` (20) holds it; whether the surveyor runs at all "
            "is the ship Loop row and the active preset"
        ),
    ),
    RetiredSetting(
        key="auto_disposition_max_closes_per_tick",
        reason=(
            "the per-tick close ceiling is the scanner's own protocol — "
            "`IssueDispositionScanner.max_closes_per_tick` (5) holds it. What governs autonomous "
            "closing is unchanged: the scanner is built only for the canonical core overlay, and "
            "`bulk_close_threshold` is still the safety-posture lever"
        ),
    ),
    RetiredSetting(
        key="backlog_sweep_cadence_hours",
        reason=(
            "the backlog_sweep Loop row's delay_seconds IS the cadence; a daily gate checked from a "
            "daily row is a second clock in series, which fires late rather than daily"
        ),
    ),
    RetiredSetting(
        key="dogfood_smoke_cadence_hours",
        reason=(
            "the dogfood Loop row's delay_seconds IS the cadence; a daily gate checked from a daily "
            "row is a second clock in series, which fires late rather than daily"
        ),
    ),
    RetiredSetting(
        key="self_update_cadence_hours",
        reason=(
            "the housekeeping Loop row's delay_seconds IS the cadence; an hourly marker gate checked "
            "from an hourly row is a second clock in series, which fires late rather than hourly"
        ),
    ),
    RetiredSetting(
        key="review_nag_enabled",
        reason=(
            "the owner's ruling is that a shipped-but-disabled `_enabled` flag is itself the defect — it "
            "converts 'nobody activated this' into a shippable state; chasing an unanswered review "
            "request is unconditional, and the repo-exemption guard plus the own-overlay scope read that "
            "made arming safe landed first"
        ),
    ),
    RetiredSetting(
        key="review_resume_reply_enabled",
        reason=(
            "the owner's ruling is that a shipped-but-disabled `_enabled` flag is itself the defect — it "
            "converts 'nobody activated this' into a shippable state; the in-thread ready-for-review-again "
            "reply is unconditional, behind the same repo-exemption guard the nag carries"
        ),
    ),
    RetiredSetting(
        key="auto_disposition_enabled",
        reason=(
            "the owner's ruling is that a shipped-but-disabled `_enabled` flag is itself the defect — it "
            "converts 'nobody activated this' into a shippable state; closing high-confidence dead "
            "backlog noise is unconditional; what keeps it off other people's backlogs is the canonical- "
            "core-overlay condition, never a flag"
        ),
    ),
    RetiredSetting(
        key="mr_triage_enabled",
        reason=(
            "the owner's ruling is that a shipped-but-disabled `_enabled` flag is itself the defect — it "
            "converts 'nobody activated this' into a shippable state; the merge-request triage survey is "
            "unconditional; it surfaces verdicts and has no post or dispatch path, so it cannot act"
        ),
    ),
    RetiredSetting(
        key="mr_conflict_scan_enabled",
        reason=(
            "the owner's ruling is that a shipped-but-disabled `_enabled` flag is itself the defect — it "
            "converts 'nobody activated this' into a shippable state; the open-merge-request conflict "
            "sweep is unconditional; its one forge merge-state read per open merge request is a cost the "
            "ship pass now always pays"
        ),
    ),
    RetiredSetting(
        key="gitlab_approval_scanner_enabled",
        reason=(
            "the owner's ruling is that a shipped-but-disabled `_enabled` flag is itself the defect — it "
            "converts 'nobody activated this' into a shippable state; the GitLab approval poll is "
            "unconditional; it emits the signals the webhook path emits, deduped on the head SHA, and "
            "merges nothing itself"
        ),
    ),
    RetiredSetting(
        key="auto_update_reinstall",
        reason=(
            "the owner's ruling is that a shipped-but-disabled `_enabled` flag is itself the defect — it "
            "converts 'nobody activated this' into a shippable state; an actual self-update always queues "
            "its deferred reinstall, riding exactly the tree the pull gate admitted — verified-green "
            "under the `auto_update_require_green_main` default, and unverified where an operator turns "
            "that off, which the outcome's reason records either way"
        ),
    ),
)
