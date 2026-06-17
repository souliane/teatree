"""Add the prompt-XOR-script DB constraint (Phase 0), after 0080 backfills rows.

Every existing row was made XOR-valid by 0080, so the check constraint applies
cleanly: a loop holds exactly one of ``prompt`` or ``script``, never both, never
neither.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0080_loop_backfill_prompt_script"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="loop",
            constraint=models.CheckConstraint(
                condition=models.Q(prompt="", script__gt="") | models.Q(prompt__gt="", script=""),
                name="loop_prompt_xor_script",
            ),
        ),
    ]
