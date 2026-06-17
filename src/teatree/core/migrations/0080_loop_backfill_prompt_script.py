"""Backfill every seeded loop to satisfy the prompt-XOR-script split (Phase 0).

``arch_review`` keeps its prompt — it is the one loop whose instruction lives in
the prompt. Every other seeded loop moves to the shared script entry point:
``script`` is set and ``prompt`` cleared, so each row holds exactly one of the
two. Reversible — the reverse restores each moved row's seed prompt.
"""

from django.db import migrations

_SCRIPT_ENTRY_POINT = "src/teatree/loops/run.py"
_PROMPT_LOOPS = frozenset({"arch_review"})


def _backfill(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    for loop in loop_model.objects.exclude(name__in=_PROMPT_LOOPS):
        loop.script = _SCRIPT_ENTRY_POINT
        loop.prompt = ""
        loop.save(update_fields=["script", "prompt"])


def _restore(apps, schema_editor) -> None:
    loop_model = apps.get_model("core", "Loop")
    for loop in loop_model.objects.exclude(name__in=_PROMPT_LOOPS):
        loop.prompt = f"Run a sub-agent to run the {loop.name} loop."
        loop.script = ""
        loop.save(update_fields=["script", "prompt"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0079_loop_script_fields"),
    ]

    operations = [
        migrations.RunPython(_backfill, _restore),
    ]
