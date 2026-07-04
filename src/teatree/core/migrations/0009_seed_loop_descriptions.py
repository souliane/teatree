"""Backfill ``Loop.description`` (and the arch_review ``Prompt.description``).

The default loops were seeded before ``Loop.description`` carried a real value, so
an existing install's rows hold a blank description. This data migration backfills
each default loop's description from the canonical set, and replaces the retired
``Default loop prompt for ...`` placeholder on the arch_review prompt with the loop's
real description.

A migration is frozen history and must not import the evolving ``teatree.loops.seed``
module, so the descriptions are INLINED here. ``tests/teatree_core/
test_loop_description_migration.py`` pins this inlined map against the canonical seed
so the migrate-path and the install-seed cannot drift.

Idempotent + non-clobbering: only rows whose ``description`` is blank are backfilled
(and the arch_review prompt only when blank or the old placeholder), so a re-run is a
no-op and an operator-rewritten description is never overwritten.
"""

from django.db import migrations

_LOOP_DESCRIPTIONS = {
    "inbox": "Drains inbound Slack mentions, DMs, review-intent and RED-CARD reactions (plus the Notion view) into the DB every 1m and routes them.",
    "idle_stack_reaper": "Stops local dev stacks left idle past their threshold to free a concurrency slot; checks every 1m.",
    "local_stack_queue": "Drains the local-stack acquisition queue, starting the next queued worktree stack whose backoff retry is due; checks every 1m.",
    "resource_pressure": "Auto-frees host disk and RAM when they cross the pressure threshold; checks every 1m on its own ~5m internal cadence.",
    "snapshot_warmer": "Refreshes each overlay-declared reference DB's DSLR snapshot out-of-band once a day so a ticket-critical-path provision never pays the slow restore+migrate path.",
    "dispatch": "Runs the always-on global scanners every 5m: dispatches pending headless Tasks to phase sub-agents, ingests incoming events, redelivers undelivered notifies, and posts deferred questions.",
    "tickets": "Scans the local Ticket DB and each code host every 5m — surfacing active and stale tickets, dispositioning issues, and marking completed ones.",
    "review": "Reviews colleague-authored open PRs every 5m and posts inline findings (with the PR-sweep, codex double-check and Slack-broadcast helpers).",
    "ship": "Sweeps your own-authored open PRs every 5m: folds in approvals/CI and executes the keystone merge of your PRs (consumes the orchestrator's MergeClear).",
    "pane_reaper": "Demotes idle Agent-Teams maker panes past the idle threshold every 5m; inert unless team mode is enabled.",
    "audit": "Verifies and posts per-overlay failed-E2E results to Slack (driven by overlay watchers) every 30m.",
    "followup": "Intakes newly-assigned issues (auto-starting ready ones) and fires the review-request nag every 30m.",
    "issue_implementer": "Discovers and claims labelled backlog issues to auto-implement, kicking off the maker pipeline; hourly, default-off behind a triple gate.",
    "housekeeping": "Fast-forwards the editable teatree and overlay installs (self-update) and pulls each overlay's main clone hourly.",
    "arch_review": "Dispatches a sub-agent every 3h to run a holistic, codebase-wide architectural review via the ac-reviewing-codebase skill.",
    "dogfood": "Runs the overlay provisioning smoke test once a day to catch broken worktree setup.",
    "eval_local": "Runs the local behavioral eval suite; the scanner enforces its own weekly cadence (checked daily).",
    "news": "Fires the daily news-scan task at 08:00 to surface relevant external releases and improvement ideas.",
    "dream": "Runs the nightly memory-consolidation pass at 03:00 — cross-link, merge, reindex MEMORY.md, decay — off the live tick.",
}

# The retired install-seed placeholder the arch_review prompt may still carry.
_ARCH_REVIEW_PROMPT_PLACEHOLDER = "Default loop prompt for 'arch_review'."


def _backfill_loop_descriptions(apps, schema_editor):
    """Set each default loop's blank description from the inlined canonical map."""
    Loop = apps.get_model("core", "Loop")
    Prompt = apps.get_model("core", "Prompt")
    for name, description in _LOOP_DESCRIPTIONS.items():
        Loop.objects.filter(name=name, description="").update(description=description)
    arch_description = _LOOP_DESCRIPTIONS["arch_review"]
    Prompt.objects.filter(name="arch_review", description__in=["", _ARCH_REVIEW_PROMPT_PLACEHOLDER]).update(
        description=arch_description,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_trustedidentity"),
    ]

    operations = [
        migrations.RunPython(_backfill_loop_descriptions, migrations.RunPython.noop),
    ]
