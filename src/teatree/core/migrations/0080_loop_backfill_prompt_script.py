"""Backfill every seeded loop to satisfy the prompt-XOR-script split (Phase 0).

``arch_review`` keeps its prompt — it is the one loop whose instruction lives in
the prompt. Every other seeded loop moves to the shared script entry point:
``script`` is set and ``prompt`` cleared, so each row holds exactly one of the
two. The migration only touches known seeded loop names that still have their
expected seeded prompt/script state, so custom or operator-edited rows are left
alone. Reversible — the reverse restores each moved row's seed prompt only from
the expected script state.
"""

from django.db import migrations

_SCRIPT_ENTRY_POINT = "src/teatree/loops/run.py"
_PROMPT_LOOPS = frozenset({"arch_review"})
_SEEDED_LOOP_NAMES = frozenset(
    {
        "inbox",
        "idle_stack_reaper",
        "local_stack_queue",
        "resource_pressure",
        "dispatch",
        "tickets",
        "review",
        "ship",
        "pane_reaper",
        "issue_implementer",
        "housekeeping",
        "audit",
        "followup",
        "arch_review",
        "dogfood",
        "eval_local",
        "news",
        "dream",
        "slack_answer",
    },
)
_SCRIPT_LOOP_NAMES = _SEEDED_LOOP_NAMES - _PROMPT_LOOPS


def _seed_prompt(name):
    return f"Run a sub-agent to run the {name} loop."


def _backfill(apps, schema_editor) -> None:
    Loop = apps.get_model("core", "Loop")
    for name in _SCRIPT_LOOP_NAMES:
        Loop.objects.filter(name=name, prompt=_seed_prompt(name), script="").update(
            script=_SCRIPT_ENTRY_POINT,
            prompt="",
        )


def _restore(apps, schema_editor) -> None:
    Loop = apps.get_model("core", "Loop")
    for name in _SCRIPT_LOOP_NAMES:
        Loop.objects.filter(name=name, prompt="", script=_SCRIPT_ENTRY_POINT).update(
            prompt=_seed_prompt(name),
            script="",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0079_loop_script_fields"),
    ]

    operations = [
        migrations.RunPython(_backfill, _restore),
    ]
